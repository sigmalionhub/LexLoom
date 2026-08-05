"""Ingest as a background job: admission, staged progress, and the write.

Three things live here, and each answers a different question.

`ensure_ingest_admitted` answers "may this organisation start an ingest right
now?" — both ceilings (one ingest per organisation, `INGEST_ORG_CAP`
organisations holding a corpus at once), read from Postgres rather than from
Redis. Both ceilings are durable facts (rows) rather than a value in a cache
`allkeys-lru` can evict under memory pressure with no log line — see
`docker-compose.yml:40` and `limits.py`'s own comment on the same risk for
the rate-limit counter. Called from the API, before a task is even queued, and
it *reserves* the slot it counts rather than inferring one from committed
state — see its own docstring and migration 0008 for what inferring cost.

The residual, and it is a documented limitation rather than a mechanism.
`INGEST_ORG_CAP` is global, so filling it denies the feature to everyone, and
one address can fill it: 30 demo logins per hour is well over the eight
organisations the default cap allows, and since a tenant may now ingest every
document in the catalogue, one login buys as many ingests as there are sources
rather than one. That halves the effective cost of the attack the paragraph
below describes; it does not change which mechanism bounds it. `_INGEST_ORG_TTL_SECONDS` shortens an
ingesting tenant's life from 24 hours to 2, which turns a one-shot day-long
outage into one that has to be re-mounted every couple of hours, and the
liveness term in `count_orgs_with_corpus()` (0008) makes the slot come back
when the TTL lapses rather than at the next hourly purge. Neither stops a
sustained attacker. The two mechanisms that would were both weighed and both
refused: evicting the oldest ingested corpus when the cap is reached destroys a
live visitor's tenant mid-session and hands an attacker a way to target
*specific* victims rather than merely the queue; a per-address cap does not
bound the global memory the cap exists for, and would need the ingest route to
grow the same trusted-proxy plumbing the demo-login limiter has, which is new
surface (`limits.py`) for a ceiling that is not the one being defended. What
actually bounds this attack is the demo-login rate limit, and that is where a
future fix belongs.

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

from app.chat.embedding import DIMENSIONS
from app.config import Settings, get_settings
from app.db.session import tenant_session
from app.errors import ProblemError
from app.ingest.catalogue import CATALOGUE

logger = structlog.get_logger(__name__)

# Every key this module writes starts here — its own namespace, sibling to
# `limits.py`'s `lexloom:limits:`, not inside it: this hash is progress, not
# admission control, and the two must stay tellable apart by prefix alone on
# a Redis shared with Celery's own broker keys.
#
# The `source_key` segment arrived when a tenant became able to hold more than
# one document. Without it two ingests for the same organisation write the same
# hash and each reports the other's stage. The segment is safe to interpolate
# because `api/v1/ingest.py` rejects any key that is not in `CATALOGUE` before
# anything here is reached — that ordering is load-bearing and is asserted by
# `test_an_unknown_source_key_never_reaches_redis`.
_PROGRESS_KEY = "lexloom:ingest:{org_id}:{source_key}"

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
#
# `FOR KEY SHARE`, and it closes the residual four previous derivations each
# concluded was only narrowed. Without it this is a read: the purge could
# delete the organisation between this SELECT and the INSERT that follows it,
# and — worse — an ingest committing strictly *between* `purge_expired_orgs`'s
# two statements (0004) left chunks that statement 2's fresh snapshot had
# already passed, unreachable forever and counted against `INGEST_ORG_CAP`.
#
# `FOR KEY SHARE` is the weakest row lock Postgres has and it conflicts with
# exactly one thing: `FOR UPDATE`, which is the strength a `DELETE` takes. So
# the two orderings are now both safe, rather than one of them being narrow:
#
#   lock first  — the purge's `DELETE FROM organizations` blocks until this
#                 transaction commits its chunks. It then deletes the
#                 organisation, and statement 2 takes a *fresh* snapshot
#                 (0004's whole reason for being two statements) in which
#                 those chunks are visible, so they are collected.
#   purge first — this SELECT blocks on the purge's uncommitted delete, and
#                 when the purge commits it re-evaluates and finds no row, so
#                 `IngestAbandoned` is raised and nothing is written.
#
# It costs the purge a wait bounded by one INSERT (the embedding happens
# before this transaction opens), and it introduces no new deadlock: this
# takes `organizations` before `document_chunks`, which is the order
# `purge_expired_orgs` already takes them in.
#
# Permitted for both roles this runs as — a locking clause needs UPDATE on the
# table, and `deploy/postgres/init/01-roles.sh` grants it to `lexloom_app` and
# `lexloom_worker` alike. Checked rather than assumed:
# `test_the_liveness_guard_can_take_its_row_lock_as_the_worker_role`.
_ALIVE_SQL = text(
    "SELECT 1 FROM organizations "
    " WHERE id = :org AND (expires_at IS NULL OR expires_at > now()) "
    " FOR KEY SHARE"
)
_ALREADY_INGESTED_SQL = text(
    "SELECT EXISTS (SELECT 1 FROM document_chunks WHERE source_key = :key)"
)

# Migration 0008. Takes no organisation: it reads `app.current_org_id` itself,
# so it cannot be pointed at a tenant the caller is not already inside.
_RESERVE_SLOT_SQL = text("SELECT public.reserve_ingest_slot(:cap, :reservation_ttl, :org_ttl)")

# Cleared in the same transaction as the INSERT, which is what keeps
# "organisations holding a corpus" and "organisations holding a reservation"
# disjoint — see migration 0008 on why that matters for the count.
_RELEASE_SLOT_SQL = text("UPDATE organizations SET ingest_reserved_at = NULL WHERE id = :org")

# How long an admitted-but-unfinished ingest keeps its slot. It has to exceed
# the worst case a queued job can actually take, and that worst case doubled
# when a tenant became able to ingest more than one document: `INGEST_ORG_CAP`
# tenants x 2 sources x `task_time_limit` = 360 s is 8 x 2 x 360 = 96 minutes at
# the default cap of 8, so the previous hour is now *under* the backlog it was
# sized against and a job still in the queue would lose its slot to a newcomer
# while it was about to run. Two hours is the smallest round number above 96
# minutes. Too long and a worker that died holds a slot for no reason; that
# direction is the safe one, because the reservation is cleared in the same
# transaction as the write.
#
# The previous derivation read "8 x 360 s = 48 minutes ... so an hour is the
# smallest round number above it", which was correct for one document per
# tenant. `config.py` carries the same arithmetic and moves with it.
_RESERVATION_TTL_SECONDS = 7200

# What an organisation's TTL is shortened to when it starts an ingest — never
# extended (migration 0008 uses LEAST), and never applied to an organisation
# that has no expiry.
#
# This is the answer to "one visitor can switch the headline feature off for a
# day". Eight demo logins plus eight ingests fill `INGEST_ORG_CAP`, and demo
# organisations live 24 hours, so every later visitor got `503
# capacity_unavailable` until the next day at negligible cost to whoever did it.
# Two hours instead of twenty-four turns a one-shot outage into one that has to
# be continuously re-mounted, and it costs a real visitor nothing measurable:
# their token dies after `DEMO_TOKEN_TTL_SECONDS` (one hour) anyway, and logging
# in again gives them a fresh organisation rather than this one.
#
# **It must stay strictly greater than `DEMO_TOKEN_TTL_SECONDS`**, and that is a
# constraint from migration 0004 rather than a preference: 0004's residual
# argument for API-path child rows rests on no live token existing when an
# organisation expires. A token is minted at login and this TTL starts at
# admission, which is later, so any TTL above the token's preserves it —
# `test_an_ingesting_org_still_outlives_every_token_for_it` holds the two
# numbers to that.
#
# What this does *not* fix is a sustained attacker: 30 logins per hour per
# address is enough to keep eight slots occupied indefinitely from one IP. See
# the module docstring.
_INGEST_ORG_TTL_SECONDS = 7200

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
    "(id, org_id, content, page_from, page_to, locator, source_key, chunk_ordinal, embedding) "
    "VALUES (:id, :org_id, :content, :page_from, :page_to, :locator, :source_key, "
    ":chunk_ordinal, :embedding)"
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


def _progress_key(org_id: str, source_key: str) -> str:
    return _PROGRESS_KEY.format(org_id=org_id, source_key=source_key)


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


async def _write_progress(org_id: str, source_key: str, **fields: object) -> None:
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
    key = _progress_key(org_id, source_key)
    mapping = {name: "" if value is None else str(value) for name, value in fields.items()}
    try:
        async with _redis(settings) as redis:
            async with redis.pipeline(transaction=True) as pipe:
                pipe.hset(key, mapping=mapping)
                pipe.expire(key, _PROGRESS_TTL_SECONDS)
                await pipe.execute()
    except (RedisError, OSError):
        logger.warning("ingest_progress_not_recorded", org_id=org_id, fields=sorted(fields))


async def mark_ingest_queued(org_id: str, source_key: str) -> None:
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
    key = _progress_key(org_id, source_key)
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


async def _rows_fallback_status(org_id: str, source_key: str) -> dict[str, object]:
    """What `read_ingest_status` returns when the progress hash is absent —
    evicted, expired, or an ingest that never wrote one. Answers from the
    same durable fact both ceilings read: a chunk actually present means
    `complete`, and its absence means `idle`, whatever Redis remembers.
    """
    async with tenant_session(org_id) as session:
        count = await session.scalar(
            text("SELECT count(*) FROM document_chunks WHERE source_key = :key"),
            {"key": source_key},
        )
    return {
        "stage": "complete" if count else "idle",
        "chunks_done": count or 0,
        "chunks_total": count or 0,
        "error": None,
        "started_at": None,
    }


async def read_ingest_status(org_id: str, source_key: str) -> dict[str, object]:
    """This organisation's ingest, as a caller would want to render it.

    Never treats a missing or unreadable hash as an error — it falls back to
    the rows. That is the entire reason the hash is allowed to be
    best-effort: nothing downstream of this function can tell "evicted" from
    "never started" apart, and nothing needs to.
    """
    settings = get_settings()
    key = _progress_key(org_id, source_key)
    try:
        async with _redis(settings) as redis:
            raw = await redis.hgetall(key)
    except (RedisError, OSError):
        logger.warning("ingest_progress_unreadable", org_id=org_id)
        raw = {}

    if not raw:
        return await _rows_fallback_status(org_id, source_key)

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


async def ensure_ingest_admitted(org_id: str, source_key: str) -> None:
    """Both ceilings, read from the rows themselves rather than from a counter.

    A Redis key would be cheaper and would be a ceiling that `allkeys-lru`
    can switch off under memory pressure, with no log line and no way to
    tell afterwards. The rows are the fact both ceilings are actually about,
    they cannot be evicted, and they are removed by the same purge that makes
    the ceiling meaningful.

    The global cap **reserves** a slot here rather than inferring one from
    committed state, and this reverses what an earlier version of this
    docstring argued. It said the concurrent-admission race was bounded by
    "the number of concurrent admissions" and priced a fix at "a lock or a
    serialised admission path", too dear for ~1.2 MB per tenant. Both halves
    were wrong. Nothing commits *during* admission, so the reading it was
    based on — `count_orgs_with_corpus()` — could not move no matter how many
    admissions passed: measured with two free slots, twelve of twelve
    consecutive POSTs were admitted, and `_guard_write_transaction` never
    re-checks the global cap, so the whole queue then committed. And the cost
    was never the megabytes: it was ~75 s of pinned CPU per queued job on a
    two-vCPU box shared with someone else's production, with
    `purge_expired_orgs` waiting behind all of it on the same queue.

    So `public.reserve_ingest_slot` (migration 0008) takes an advisory lock,
    counts organisations holding a corpus *or* a live reservation, and marks
    this one — one statement, no window. Both checks now run in the same tenant
    transaction: the reservation is committed only if the per-organisation
    check passed, and it is rolled back rather than left behind if anything
    below it raises.

    A duplicate for the *same* organisation still cannot pass — see
    `_guard_write_transaction`, which repeats the per-organisation check inside
    the transaction that writes.
    """
    settings = get_settings()
    async with tenant_session(org_id) as session:
        already = await session.scalar(_ALREADY_INGESTED_SQL, {"key": source_key})
        if already:
            raise _already_ingested()
        reserved = await session.scalar(
            _RESERVE_SLOT_SQL,
            {
                "cap": settings.ingest_org_cap,
                "reservation_ttl": _RESERVATION_TTL_SECONDS,
                "org_ttl": _INGEST_ORG_TTL_SECONDS,
            },
        )

    # Raised outside the session, so the reservation the call above may have
    # written is committed on the way out rather than rolled back by the
    # exception. `false` means nothing was written, so the rollback that a
    # raise inside would cause is not what distinguishes the two paths — the
    # ordering does, and it is worth not having to think about twice.
    if not reserved:
        raise _capacity_unavailable(_INGEST_RETRY_AFTER_SECONDS)


async def _guard_write_transaction(session: AsyncSession, org_id: str, source_key: str) -> None:
    """Both guards, evaluated on `session`'s connection and snapshot.

    Called twice per job: once as a fast-path before parsing and embedding
    even start (its own short-lived session, own snapshot — see the module
    docstring's note on why that is only an optimisation), and once
    authoritatively inside the same transaction that performs the INSERT.

    The liveness check takes a `FOR KEY SHARE` row lock, so on the
    authoritative call the purge cannot run past this organisation while the
    chunks are being written — see `_ALIVE_SQL`. On the fast-path call the lock
    is released microseconds later with that short transaction, which is
    harmless and is why the same statement serves both.
    """
    alive = await session.scalar(_ALIVE_SQL, {"org": org_id})
    if not alive:
        raise IngestAbandoned(org_id)
    already = await session.scalar(_ALREADY_INGESTED_SQL, {"key": source_key})
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
        await _guard_write_transaction(session, org_id, source_key)

    await _write_progress(org_id, source_key, stage="parsing")

    from app.ingest.parse import parse_source

    chunks = parse_source(source)

    await _write_progress(org_id, source_key, stage="chunking", chunks_total=len(chunks))

    from app.chat.embedding import get_embedder

    embedder = get_embedder()
    vectors: list[list[float]] = []
    await _write_progress(org_id, source_key, stage="embedding")
    for start in range(0, len(chunks), EMBED_BATCH):
        batch = chunks[start : start + EMBED_BATCH]
        vectors.extend(embedder.encode([chunk.content for chunk in batch]))
        await _write_progress(org_id, source_key, chunks_done=len(vectors))

    await _write_progress(org_id, source_key, stage="indexing")

    async with tenant_session(org_id) as session:
        await _guard_write_transaction(session, org_id, source_key)
        await session.execute(
            _INSERT_CHUNK,
            [
                {
                    "id": str(uuid.uuid4()),
                    "org_id": org_id,
                    "content": chunk.content,
                    "page_from": chunk.page_from,
                    "page_to": chunk.page_to,
                    # One of the two is always set and the other always NULL:
                    # a paginated source says where a chunk is with pages, a
                    # `.docx` says it with its heading path. `parse_source`
                    # decides which by dispatching on `source.media_type`.
                    "locator": chunk.locator,
                    "source_key": source_key,
                    "chunk_ordinal": chunk.chunk_ordinal,
                    "embedding": vector,
                }
                for chunk, vector in zip(chunks, vectors, strict=True)
            ],
        )
        # The slot changes hands here, atomically: this organisation stops
        # holding a *reservation* in the same transaction in which it starts
        # holding a *corpus*. `reserve_ingest_slot` adds the two counts
        # together (migration 0008), so an overlap would make one tenant
        # occupy two slots until the reservation aged out. Rolled back with
        # the INSERT if this transaction fails, which is the state a retry
        # needs: the reservation is still there and still this tenant's.
        await session.execute(_RELEASE_SLOT_SQL, {"org": org_id})

    await _write_progress(org_id, source_key, stage="complete", chunks_done=len(chunks))
    return len(chunks)
