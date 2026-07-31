from app.worker import signals  # noqa: F401  (registers process-init handler)
from app.worker.celery_app import celery_app


@celery_app.task(name="lexloom.ping")
def ping(payload: str) -> dict[str, str]:
    """Smoke task proving the API can reach the broker and a worker consumes it."""
    return {"echo": payload, "worker": "lexloom"}


@celery_app.task(name="lexloom.purge_expired_orgs")
def purge_expired_orgs() -> int:
    """Delete demo organisations past their TTL. Runs on a beat schedule.

    The work itself lives in app.auth.demo next to the TTL that creates the
    expiry, so the thing that stamps `expires_at` and the thing that acts on it
    cannot drift.
    """
    import asyncio

    from app.auth.demo import purge_expired_orgs as _purge
    from app.db.engine import engine

    async def _run() -> int:
        try:
            return await _purge()
        finally:
            # Dispose inside the same loop that opened the connections, for the
            # reason spelled out in signals.py: asyncio.run closes its loop on
            # return, and the *next* invocation of this task opens a new one.
            # A pooled connection surviving that handover fails later, in
            # another task, with a traceback pointing at the wrong code.
            await engine.dispose()

    return asyncio.run(_run())
