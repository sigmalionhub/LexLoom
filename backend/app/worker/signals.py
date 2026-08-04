import structlog
from celery.signals import worker_process_init

logger = structlog.get_logger(__name__)


@worker_process_init.connect
def on_worker_process_init(**_: object) -> None:
    """Runs in each prefork child, after fork.

    Anything holding OS resources must be created here, never at module
    import: a fork copies file descriptors and thread-pool bookkeeping but
    not the threads themselves. In M2 this is also where the ONNX embedding
    model is loaded.

    For the model the reason is a documented caution, and it is worth being
    exact about that, because two stronger reasons were tested for task 19 and
    NEITHER of them held.

    The caution that stands is the runtime's own. Building an InferenceSession
    is what makes this process multi-threaded, and CPython warns that `fork()`
    in a multi-threaded process may deadlock in the child — the probe below
    emitted exactly that DeprecationWarning. Celery forks after loading the
    application, so a model built at import is a model built before that fork.
    Loading in this handler is what keeps the parent single-threaded on our
    account. A documented hazard plus a warning actually observed is a real
    reason; it is not the same thing as a reproduced failure, and this docstring
    does not pretend otherwise.

    What was NOT reproduced, recorded here so nobody re-derives it from the
    comment history:

    * The hang. A parent-built session embedded correctly in the child at
      fastembed 0.5.1, at OMP_NUM_THREADS=1 and at 4.
    * Memory duplication. An earlier revision of this docstring claimed each
      child pays for its own ~216 MiB because ONNX writes into its arena.
      Measured directly, that is false: a forked child shows 2 MiB private
      against 147 MiB shared immediately after the fork, and 4 MiB private
      after its first inference — copy-on-write holds for the weights. What is
      private is batch activation memory, 119 MiB at a batch of 32, and the
      child pays that wherever the model was built.

    So this is a conservative default rather than a fix for a demonstrated
    failure, and it is cheap: one load per child, and children are recycled
    only every `--max-tasks-per-child=50`. Note that the memory measurement
    mildly favours the opposite arrangement — children would share a
    parent-built model — so nobody should reach for this pattern expecting it
    to save memory. See the task 19 report.
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
    # across the fork are released first, and only then does the model become
    # resident. Doing it the other way round holds both at once for no reason,
    # on a box where that margin is most of the budget.
    #
    # Imported inside the handler, and `app.chat.embedding` in turn imports
    # fastembed inside `_load()`, so the parent never touches ONNX at all. Why
    # that matters — and what did and did not reproduce — is in the docstring.
    #
    # Eagerly rather than on first use, so compiling the graph is paid at worker
    # start-up rather than inside the first task, where it would count against
    # `task_soft_time_limit`.
    from app.chat.embedding import get_embedder

    get_embedder()._ensure()

    logger.info("worker_process_initialised", embedding_model_loaded=True)
