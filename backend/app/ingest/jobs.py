"""Ingest as a background job: admission, staged progress, and the write.

Three things live here, and each answers a different question.

`ensure_ingest_admitted` answers "may this organisation start an ingest right
now?" — both ceilings (one ingest per organisation, `INGEST_ORG_CAP`
organisations holding a corpus at once), read from Postgres rather than from
Redis. Both ceilings are durable facts (rows) rather than a value in a cache
`allkeys-lru` can evict under memory pressure with no log line — see
`docker-compose.yml:40` and `limits.py`'s own comment on the same risk for
the rate-limit counter. Called from the API, before a task is even queued.

`run_ingest` is the job body a Celery task in `app/worker/tasks.py` runs
inside `asyncio.run()`. It is not the task itself: the task owns the
asyncio-loop-and-engine-disposal convention every task in this file follows,
and this module has no opinion about that.

`read_ingest_status` answers "what is this organisation's ingest doing right
now?" from a Redis hash that is progress and only progress — losing it costs
a progress bar, never a wrong answer, because it falls back to the rows
themselves for anything a caller could act on.
"""

from __future__ import annotations

import time
import uuid

import redis.asyncio as aioredis
import structlog
from pgvector.sqlalchemy import Vector
from redis.exceptions import RedisError
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.corpus import SEED_SOURCE_KEY
from app.chat.embedding import DIMENSIONS
from app.config import Settings, get_settings
from app.db.session import system_session, tenant_session
from app.errors import ProblemError
from app.ingest.catalogue import CATALOGUE

logger = structlog.get_logger(__name__)

# Every key this module writes starts here — its own namespace, sibling to
# `limits.py`'s `lexloom:limits:`, not inside it: this hash is progress, not
# admission control, and the two must stay tellable apart by prefix alone on
# a Redis shared with Celery's own broker keys.
_PROGRESS_KEY = "lexloom:ingest:{org_id}"

# An order of magnitude above the job's own runtime (task_time_limit=360,
# celery_app.py), so a status poll during a retry still finds the hash, and
# short enough that a crashed job's state does not outlive anyone's interest
# in it. Matches the TTL `StreamSlot` and the rate-limit window use for the
# same reason: a number sized against how long a human keeps looking, not
# against the data's own lifetime.
_PROGRESS_TTL_SECONDS = 3600

# Told to the client on a 503; not a promise the ceiling relaxes that soon.
# The cap only actually relaxes when the hourly purge collects an expired
# organisation (celery_app.py's beat schedule), so nothing shorter than that
# is honest — but telling a client to wait an hour invites it to give up and
# never come back, and the ceiling genuinely can free early (an organisation
# purged early is possible, if not scheduled). This is a floor for a polite
# client, not a forecast.
_INGEST_RETRY_AFTER_SECONDS = 300

# 32, and it is a ceiling rather than a default to tune upward. Measured for
# task 19 against this model: resident cost is flat to ~8 texts, 253 MiB at
# 16, 315 MiB at 32 and 444 MiB at 64, against a worker container limited to
# 650m (docker-compose.yml). Raising it means re-measuring first — see the
# same constant and the same comment in app/worker/tasks.py, which
# `embed_org_corpus` uses; the two are kept as separate constants rather than
# one shared import because the two tasks are allowed to diverge on this
# number in the future without one edit silently changing the other's memory
# budget.
EMBED_BATCH = 32

# The two guards run inside the transaction that writes the chunks
# (`_guard_write_transaction`), and also — as a fast-path only — before the
# expensive parse-and-embed work below even starts. The fast path exists so a
# job whose organisation is already gone, or which lost a same-organisation
# race, fails in milliseconds instead of after minutes of embedding; it is an
# optimisation, not the guarantee. The guarantee is the second call, inside
# the same transaction as the INSERT, because real time passes during
# parsing and embedding and an organisation's TTL can lapse inside that
# window even though the worker itself never runs two tasks at once
# (--concurrency=1): a check taken only at the start would not see a TTL
# that expired an hour into an unusually large job.
_ALIVE_SQL = text(
    "SELECT EXISTS (SELECT 1 FROM organizations "
    " WHERE id = :org AND (expires_at IS NULL OR expires_at > now()))"
)
_ALREADY_INGESTED_SQL = text(
    "SELECT EXISTS (SELECT 1 FROM document_chunks "
    " WHERE source_key IS NOT NULL AND source_key <> :seed)"
)

# The same eight columns `app/chat/corpus.py`'s `_INSERT_CHUNK` writes, for
# the same reasons: `bindparam(..., type_=Vector(DIMENSIONS))` because
# `register_vector` is called nowhere in this repository and asyncpg has no
# codec for a bare `list[float]`; `DIMENSIONS` rather than a literal 384 so
# this width and the embedder's are the same number rather than two copies
# of it. A separate statement from corpus.py's rather than a shared import:
# that one is single-owner *for the seed corpus's two callers*
# (`copy_corpus_into` and `seed_public_corpus`) specifically so a column-list
# edit reaches both without a second copy drifting — this INSERT is a third,
# unrelated caller, writing organisation-generated content rather than the
# seed file, and importing across that boundary would couple two features
# that have no reason to change together.
_INSERT_CHUNK = text(
    "INSERT INTO document_chunks "
    "(id, org_id, content, page_from, page_to, source_key, chunk_ordinal, embedding) "
    "VALUES (:id, :org_id, :content, :page_from, :page_to, :source_key, :chunk_ordinal, :embedding)"
).bindparams(bindparam("embedding", type_=Vector(DIMENSIONS)))


class IngestAbandoned(RuntimeError):
    """The organisation no longer exists, or has expired, by the time the
    write transaction runs. Not necessarily a bug in the caller: the purge
    and this job both run off the same queue (Step 6), and an organisation's
    24h TTL can lapse during an unusually large or delayed ingest."""


class IngestAlreadyComplete(RuntimeError):
    """A second ingest task for the same organisation reached the write
    transaction after the first one committed. `ensure_ingest_admitted`'s own
    check can be raced by two POSTs before either commits; this is the guard
    the worker's own `--concurrency=1` makes airtight for one organisation,
    because it runs inside the same transaction as the INSERT rather than as
    a separate statement that could observe a stale answer."""


def _progress_key(org_id: str) -> str:
    return _PROGRESS_KEY.format(org_id=org_id)


def _redis(settings: Settings) -> aioredis.Redis:
    """A client for one operation, closed by the caller's `async with`.

    `decode_responses=True`, unlike `limits.py`'s equivalent: that module
    only ever pipelines integers and opaque tokens, this one reads back a
    hash of strings a caller has to parse. Not a module-level singleton for
    the same reason as `limits.py`'s: a connection pool belongs to the event
    loop that created it, and each Celery task invocation gets its own loop
    via `asyncio.run()`.
    """
    return aioredis.from_url(settings.redis_url, decode_responses=True)


async def _write_progress(org_id: str, **fields: object) -> None:
    """Merge-update the progress hash and refresh its TTL.

    Best-effort and silent on failure: `allkeys-lru` may evict this key at
    any moment under memory pressure, and what that costs is a stale
    progress bar, not a wrong answer anywhere — `read_ingest_status` falls
    back to the rows for anything a caller could act on. Raising here would
    make a Redis blip fail an ingest that is otherwise proceeding correctly.
    """
    if not fields:
        return
    settings = get_settings()
    key = _progress_key(org_id)
    mapping = {name: "" if value is None else str(value) for name, value in fields.items()}
    try:
        async with _redis(settings) as redis:
            async with redis.pipeline(transaction=True) as pipe:
                pipe.hset(key, mapping=mapping)
                pipe.expire(key, _PROGRESS_TTL_SECONDS)
                await pipe.execute()
    except (RedisError, OSError):
        logger.warning("ingest_progress_not_recorded", org_id=org_id, fields=sorted(fields))


async def mark_ingest_queued(org_id: str) -> None:
    """Reset the progress hash for a fresh attempt. Called from the admission
    path, before the task is enqueued — not from the worker.

    A status poll landing between the POST returning and the worker actually
    starting the task must see `queued` rather than a missing hash, or
    `read_ingest_status`'s rows-fallback would report `idle` for however long
    the queue backlog runs — up to `INGEST_ORG_CAP` x `task_time_limit`
    (see migration 0007 and Step 6 of the task 23 brief).

    Deletes the hash before writing rather than merging into whatever is
    already there: a previous failed attempt for this same organisation (one
    whose write transaction never committed, so `ensure_ingest_admitted`
    still permits a retry) can leave `stage=error` and a stale `error` field
    behind, and a plain HSET would not clear fields it does not mention.
    """
    settings = get_settings()
    key = _progress_key(org_id)
    mapping = {
        "stage": "queued",
        "chunks_done": "0",
        "chunks_total": "0",
        "error": "",
        "started_at": str(int(time.time())),
    }
    try:
        async with _redis(settings) as redis:
            async with redis.pipeline(transaction=True) as pipe:
                pipe.delete(key)
                pipe.hset(key, mapping=mapping)
                pipe.expire(key, _PROGRESS_TTL_SECONDS)
                await pipe.execute()
    except (RedisError, OSError):
        logger.warning("ingest_progress_not_recorded", org_id=org_id, stage="queued")


async def _rows_fallback_status(org_id: str) -> dict[str, object]:
    """What `read_ingest_status` returns when the progress hash is absent —
    evicted, expired, or an ingest that never wrote one. Answers from the
    same durable fact both ceilings read: a chunk actually present means
    `complete`, and its absence means `idle`, whatever Redis remembers.
    """
    async with tenant_session(org_id) as session:
        count = await session.scalar(
            text(
                "SELECT count(*) FROM document_chunks "
                "WHERE source_key IS NOT NULL AND source_key <> :seed"
            ),
            {"seed": SEED_SOURCE_KEY},
        )
    return {
        "stage": "complete" if count else "idle",
        "chunks_done": count or 0,
        "chunks_total": count or 0,
        "error": None,
        "started_at": None,
    }


async def read_ingest_status(org_id: str) -> dict[str, object]:
    """This organisation's ingest, as a caller would want to render it.

    Never treats a missing or unreadable hash as an error — it falls back to
    the rows. That is the entire reason the hash is allowed to be
    best-effort: nothing downstream of this function can tell "evicted" from
    "never started" apart, and nothing needs to.
    """
    settings = get_settings()
    key = _progress_key(org_id)
    try:
        async with _redis(settings) as redis:
            raw = await redis.hgetall(key)
    except (RedisError, OSError):
        logger.warning("ingest_progress_unreadable", org_id=org_id)
        raw = {}

    if not raw:
        return await _rows_fallback_status(org_id)

    return {
        "stage": raw.get("stage", "idle"),
        "chunks_done": int(raw.get("chunks_done") or 0),
        "chunks_total": int(raw.get("chunks_total") or 0),
        "error": raw.get("error") or None,
        "started_at": int(raw["started_at"]) if raw.get("started_at") else None,
    }


def _already_ingested() -> ProblemError:
    return ProblemError(
        code="already_ingested",
        status=409,
        title="Already ingested",
        # Not "retry after": a 409 here means retrying changes nothing until
        # the organisation itself is gone, which is the opposite of what
        # Retry-After promises a client.
        detail="This organisation has already ingested a document from the catalogue.",
    )


def _capacity_unavailable(retry_after: int) -> ProblemError:
    return ProblemError(
        code="capacity_unavailable",
        status=503,
        title="Capacity unavailable",
        detail="The service cannot start this ingest right now. Retry after Retry-After seconds.",
        headers={"Retry-After": str(retry_after)},
    )


async def ensure_ingest_admitted(org_id: str) -> None:
    """Both ceilings, read from the rows themselves rather than from a counter.

    A Redis key would be cheaper and would be a ceiling that `allkeys-lru`
    can switch off under memory pressure, with no log line and no way to
    tell afterwards. The rows are the fact both ceilings are actually about,
    they cannot be evicted, and they are removed by the same purge that makes
    the ceiling meaningful — so the ceiling relaxes exactly when capacity is
    freed and never before.

    A race between two concurrent POSTs for the *global* cap is possible and
    bounded: both can see `held < cap` and both proceed, so the ceiling can
    be exceeded by the number of concurrent admissions. The cost of that is
    ~1.2 MB per tenant; the cost of preventing it is a lock or a serialised
    admission path. Cheaper, and named rather than hidden. A duplicate for
    the *same* organisation cannot pass this way — see
    `_guard_write_transaction`, which repeats the per-organisation check
    inside the transaction that writes.
    """
    async with tenant_session(org_id) as session:
        already = await session.scalar(_ALREADY_INGESTED_SQL, {"seed": SEED_SOURCE_KEY})
    if already:
        raise _already_ingested()

    async with system_session() as session:
        held = await session.scalar(text("SELECT public.count_orgs_with_corpus()"))
    if held >= get_settings().ingest_org_cap:
        raise _capacity_unavailable(_INGEST_RETRY_AFTER_SECONDS)


async def _guard_write_transaction(session: AsyncSession, org_id: str) -> None:
    """Both guards, evaluated on `session`'s connection and snapshot.

    Called twice per job: once as a fast-path before parsing and embedding
    even start (its own short-lived session, own snapshot — see the module
    docstring's note on why that is only an optimisation), and once
    authoritatively inside the same transaction that performs the INSERT.
    """
    alive = await session.scalar(_ALIVE_SQL, {"org": org_id})
    if not alive:
        raise IngestAbandoned(org_id)
    already = await session.scalar(_ALREADY_INGESTED_SQL, {"seed": SEED_SOURCE_KEY})
    if already:
        raise IngestAlreadyComplete(org_id)


async def run_ingest(org_id: str, source_key: str) -> int:
    """Parse, embed and write one catalogue source into `org_id`'s corpus.

    Stages, in order, each written *before* the work it names begins — never
    after, or a status poll during a stage sees the previous one's name:
    `parsing -> chunking -> embedding -> indexing -> complete`. `queued` is
    not written here; it belongs to `mark_ingest_queued`, called from the
    admission path before this job's task is even enqueued.

    `parsing` and `chunking` are adjacent stage writes around one call
    (`parse_source`) rather than two separately-timed phases: the parser
    (`app/ingest/parse.py`, owned by Task 22) performs PDF extraction and
    chunk packing as a single pass with no seam between them to report
    progress from. Splitting it would mean changing a file this task does
    not own. Both names are still worth writing, because a caller polling
    mid-parse sees an accurate stage even though the transition between the
    two is not separately timed.

    Embedding calls `Embedder.encode` — the synchronous path, batched at
    `EMBED_BATCH` — never `aencode_query`. That async wrapper exists for the
    API's request path, where a semaphore bounds concurrent inference because
    the API runs `--workers 1` and inference would otherwise stall every
    concurrent SSE stream. This function runs inside a Celery worker task,
    where the model was already loaded in `worker_process_init`
    (`app/worker/signals.py`) after `fork()`, nothing else runs on this
    process's event loop at the same time, and there is no stream to stall.

    All embeddings are computed and held in memory (~3 MB for ~1000 chunks at
    384 floats each) *before* the write transaction opens, not written batch
    by batch as `embed_org_corpus` does. That is deliberate here and would be
    wrong there: `embed_org_corpus` backfills a corpus that already exists
    and can safely leave it partially embedded between transactions, while
    this job's whole point is that an organisation either has its complete
    corpus or does not — see `_guard_write_transaction`'s docstring on why
    the write is one transaction.
    """
    source = CATALOGUE[source_key]

    async with tenant_session(org_id) as session:
        await _guard_write_transaction(session, org_id)

    await _write_progress(org_id, stage="parsing")

    from app.ingest.parse import parse_source

    chunks = parse_source(source)

    await _write_progress(org_id, stage="chunking", chunks_total=len(chunks))

    from app.chat.embedding import get_embedder

    embedder = get_embedder()
    vectors: list[list[float]] = []
    await _write_progress(org_id, stage="embedding")
    for start in range(0, len(chunks), EMBED_BATCH):
        batch = chunks[start : start + EMBED_BATCH]
        vectors.extend(embedder.encode([chunk.content for chunk in batch]))
        await _write_progress(org_id, chunks_done=len(vectors))

    await _write_progress(org_id, stage="indexing")

    async with tenant_session(org_id) as session:
        await _guard_write_transaction(session, org_id)
        await session.execute(
            _INSERT_CHUNK,
            [
                {
                    "id": str(uuid.uuid4()),
                    "org_id": org_id,
                    "content": chunk.content,
                    "page_from": chunk.page_from,
                    "page_to": chunk.page_to,
                    "source_key": source_key,
                    "chunk_ordinal": chunk.chunk_ordinal,
                    "embedding": vector,
                }
                for chunk, vector in zip(chunks, vectors, strict=True)
            ],
        )

    await _write_progress(org_id, stage="complete", chunks_done=len(chunks))
    return len(chunks)
