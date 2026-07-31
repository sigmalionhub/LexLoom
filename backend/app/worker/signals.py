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
    logger.info("worker_process_initialised")
