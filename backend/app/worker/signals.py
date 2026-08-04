import structlog
from celery.signals import worker_process_init

logger = structlog.get_logger(__name__)


@worker_process_init.connect
def on_worker_process_init(**_: object) -> None:
    """Runs in each prefork child, after fork.

    Anything holding OS resources must be created here, never at module
    import: a fork copies file descriptors and thread-pool bookkeeping but
    not the threads themselves. In M2 this is where the ONNX embedding model
    is loaded — instantiating it in the parent makes InferenceSession hang in
    the child with no error and no traceback.
    """
    import asyncio

    from app.db.engine import assert_unprivileged_role, engine

    # Drop connections inherited across the fork WITHOUT closing them — the
    # parent still owns those sockets, and closing them from a child corrupts
    # the wire protocol for everyone.
    engine.sync_engine.dispose(close=False)

    async def _check() -> None:
        # The same guard the API applies at startup. The worker connects as
        # lexloom_worker and is subject to the same row-level security; a
        # worker misconfigured with BYPASSRLS would otherwise boot silently
        # with tenant isolation defeated and nothing would notice.
        async with engine.connect() as conn:
            await assert_unprivileged_role(conn)
        # Dispose inside the same loop that opened the connection. asyncio.run
        # closes the loop when it returns, and a pooled connection left bound
        # to a closed loop fails in confusing ways the moment the first task
        # opens a loop of its own.
        await engine.dispose()

    asyncio.run(_check())

    # After the database work above, not before it: the connections inherited
    # across the fork are released first, and only then is ~216 MiB of model
    # made resident. Doing it the other way round holds both at once for no
    # reason, on a box where that margin is most of the budget.
    #
    # Imported inside the handler, and `app.chat.embedding` in turn imports
    # fastembed inside `_load()`, so the parent never touches ONNX at all.
    #
    # The reason that is *measured* is memory: a model built in the parent is
    # inherited by every child, and ONNX Runtime writes into its arena on first
    # inference, so copy-on-write does not save the copy — each child pays
    # ~216 MiB regardless, at an unpredictable moment instead of at start-up.
    #
    # The reason in this module's docstring — that a parent-built
    # InferenceSession hangs in the child — was probed for this task and did not
    # reproduce at fastembed 0.5.1: a session built before the fork embedded
    # correctly afterwards, at OMP_NUM_THREADS=1 and at 4. It remains the
    # documented hazard of fork() plus a thread pool and the discipline is kept,
    # but it is not, on this version, a demonstrated one. See the task 19 report.
    #
    # Eagerly rather than on first use, so compiling the graph is paid at worker
    # start-up rather than inside the first task, where it would count against
    # `task_soft_time_limit`.
    from app.chat.embedding import get_embedder

    get_embedder()._ensure()

    logger.info("worker_process_initialised", embedding_model_loaded=True)
