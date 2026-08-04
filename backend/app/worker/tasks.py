from pgvector.sqlalchemy import Vector
from sqlalchemy import bindparam, text
from sqlalchemy.exc import DBAPIError

# `DIMENSIONS` only, and at module level deliberately. Importing
# `app.chat.embedding` does not import fastembed or ONNX Runtime — that is
# asserted by test_importing_the_module_does_not_import_fastembed — so the Celery
# parent stays single-threaded and its fork stays the fork CPython is happy with.
# The embedder itself is still imported inside the task body, after that fork.
from app.chat.embedding import DIMENSIONS
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


# 32, and it is a ceiling rather than a default to tune upward. Measured for
# task 19 against this model: resident cost is flat to ~8 texts, 253 MiB at 16,
# 315 MiB at 32 and 444 MiB at 64, against a worker container limited to 650m.
# Raising it means re-measuring first.
EMBED_BATCH = 32

# One statement per row, executed as an executemany. The obvious single-statement
# form — `UPDATE ... WHERE id = ANY(:ids)` — cannot carry a *different* vector per
# id without an array-of-vector bind that asyncpg has no codec for; the whole
# reason `bindparam(..., type_=Vector(DIMENSIONS))` exists here is that
# `register_vector` is called nowhere in this repository, so the driver rejects a
# raw list. An executemany keeps every value a bound parameter and stays one
# round trip. `DIMENSIONS` rather than a literal, so this width and the one the
# embedder produces are the same number and not two copies of it.
_UPDATE_EMBEDDING = text(
    "UPDATE document_chunks SET embedding = :embedding WHERE id = :id"
).bindparams(bindparam("embedding", type_=Vector(DIMENSIONS)))

# Read in the same statement shape the write uses, so the two cannot disagree
# about what "still needs embedding" means. No OFFSET: the batch that was just
# written stops matching `embedding IS NULL`, so the next pass advances by
# itself, and a row inserted while this runs is picked up rather than skipped.
_SELECT_UNEMBEDDED = text(
    "SELECT id, content FROM document_chunks "
    "WHERE embedding IS NULL ORDER BY id LIMIT :batch"
)


@celery_app.task(bind=True, name="lexloom.embed_org_corpus", max_retries=3)
def embed_org_corpus(self, org_id: str) -> int:
    """Backfill embeddings for one organisation's chunks. Returns how many.

    The path a *seeded* corpus never takes: `copy_corpus_into` writes vectors
    with the rows, from the file beside the corpus. This is for chunks that
    arrive without one — an ingested document — and for a corpus that predates
    the column being populated.

    Everything runs under `tenant_session(org_id)`, so the rows this can see and
    the rows it may write are the same set, decided by the policy rather than by
    a WHERE clause that could be forgotten. It is also why the organisation is a
    parameter: there is no "embed everything" mode, because there is no session
    that could legitimately see everything.

    Inference happens *between* transactions, never inside one. A batch is read
    and the session closed, the model runs, then a second short transaction
    writes. Holding a transaction open across ~30 seconds of ONNX inference is
    how a backfill turns into lock contention with the request path.

    Retried on deadlock and only on deadlock, matching `purge_expired_orgs`: the
    purge deletes an organisation's chunks and this updates them, so the two can
    genuinely collide. Retrying on `DBAPIError` itself would quietly retry
    syntax errors and permission denials too.
    """
    import asyncio

    from app.chat.embedding import get_embedder
    from app.db.engine import engine
    from app.db.session import tenant_session

    async def _run() -> int:
        try:
            embedder = get_embedder()
            written = 0
            seen: set[str] = set()
            while True:
                async with tenant_session(org_id) as session:
                    rows = (
                        await session.execute(_SELECT_UNEMBEDDED, {"batch": EMBED_BATCH})
                    ).all()
                if not rows:
                    return written

                # A batch that comes back a second time means the write did not
                # take — a swallowed error, a policy that admits the SELECT and
                # refuses the UPDATE — and the loop would spin on it forever,
                # embedding the same rows and reporting progress. Cheap to
                # check, and the alternative is a task that never returns.
                ids = [str(row.id) for row in rows]
                if seen.intersection(ids):
                    raise RuntimeError(
                        f"embed_org_corpus made no progress for organisation {org_id}: "
                        f"{len(seen.intersection(ids))} chunk(s) still have no embedding "
                        "after being written"
                    )
                seen.update(ids)

                vectors = embedder.encode([row.content for row in rows])
                async with tenant_session(org_id) as session:
                    await session.execute(
                        _UPDATE_EMBEDDING,
                        [
                            {"id": row.id, "embedding": vector}
                            for row, vector in zip(rows, vectors, strict=True)
                        ],
                    )
                written += len(rows)
        finally:
            # Dispose inside the same loop that opened the connections, for the
            # reason spelled out in signals.py and repeated by the purge above:
            # asyncio.run closes its loop on return, and a pooled connection
            # surviving that handover fails later, in another task, with a
            # traceback pointing at the wrong code.
            await engine.dispose()

    try:
        return asyncio.run(_run())
    except DBAPIError as exc:
        if not is_deadlock(exc):
            raise
        raise self.retry(exc=exc, countdown=5) from exc
