from sqlalchemy.exc import DBAPIError

from app.worker import signals  # noqa: F401  (registers process-init handler)
from app.worker.celery_app import celery_app

# SQLSTATE 40P01. Postgres resolves a deadlock by killing one of the parties,
# and which one is not something the loser gets a say in.
DEADLOCK_DETECTED = "40P01"


def is_deadlock(exc: BaseException) -> bool:
    """True for a deadlock, false for every other database failure.

    Matched on SQLSTATE rather than on an exception class because there is no
    class to match: SQLAlchemy surfaces an asyncpg deadlock as a bare
    `DBAPIError` — not `OperationalError`, not a deadlock-specific subclass —
    with the code only on `.orig`. Verified against a real deadlock rather than
    assumed. Retrying on `DBAPIError` itself would quietly retry syntax errors
    and permission denials too.
    """
    return isinstance(exc, DBAPIError) and (
        getattr(exc.orig, "sqlstate", None) == DEADLOCK_DETECTED
    )


@celery_app.task(name="lexloom.ping")
def ping(payload: str) -> dict[str, str]:
    """Smoke task proving the API can reach the broker and a worker consumes it."""
    return {"echo": payload, "worker": "lexloom"}


@celery_app.task(bind=True, name="lexloom.purge_expired_orgs", max_retries=3)
def purge_expired_orgs(self) -> int:
    """Delete demo organisations past their TTL. Runs on a beat schedule.

    The work itself lives in app.auth.demo next to the TTL that creates the
    expiry, so the thing that stamps `expires_at` and the thing that acts on it
    cannot drift.

    Retried on deadlock, and only on deadlock. The purge locks organisations
    before their children; a request handler that updates a message and then its
    own organisation row takes those in the opposite order, and Postgres breaks
    the cycle by killing one side — which can be this one. Because the purge is
    a single statement, losing it loses the whole run, and without a retry the
    next attempt is an hour away on the beat schedule. Retrying is the cheap
    half of the fix; reordering the locks would mean giving up the single
    statement that makes the verdict and the deletes atomic.
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

    try:
        return asyncio.run(_run())
    except DBAPIError as exc:
        if not is_deadlock(exc):
            raise
        # Countdown, not immediate: an instant retry re-enters the same lock
        # cycle against a transaction that is very likely still holding it.
        raise self.retry(exc=exc, countdown=5) from exc
