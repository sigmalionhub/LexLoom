"""The ingest job: both ceilings, staged progress, and the guarded write.

Two tests are required rather than optional (task 23 brief, Step 8): one that
the seed corpus never counts against `INGEST_ORG_CAP`, and one that the
global cap actually refuses once it is held. Both are held to their specific
failure by asserting `ProblemError.code` rather than the exception type alone
— a refusal for the wrong reason (an unreachable database, a missing
organisation) must not be mistaken for the ceiling doing its job. The task 23
brief's own sketch uses `pytest.raises(..., match=...)`; `ProblemError`
carries no `__str__` naming its `code` (`app/errors.py` — `str(exc)` is the
human-readable `detail`, not the machine-readable `code`), so `match=` on the
exception's string would not actually pin the reason it claims to. Checking
`.code` on the caught exception is the stronger form of the same intent and
is what every test below does instead.

Tests that need the real ONNX weights carry `@pytest.mark.model`; conftest
skips them when the cache is cold. The full-corpus round trip
(`test_ingest_source_embeds_and_writes_the_full_catalogue_source`) is the
slow one — measured at ~75 s wall-clock outside container CPU limits (see the
task 23 report) — hence its own `@pytest.mark.timeout`,
above the suite's 120 s default (`pyproject.toml`).
"""

import asyncio
import uuid

import pytest

# Redis database 1, not 0 — the same reason conftest's `LIMITS_TEST_REDIS_URL`
# exists: database 0 is shared with the running demo stack's own Celery
# broker and any real visitor's progress hash, so writing test keys there
# risks colliding with something that matters. Imported rather than
# hand-copied so a change to conftest's isolation database is not a second
# place to update.
from conftest import LIMITS_TEST_REDIS_URL
from sqlalchemy import text

from app.chat.corpus import copy_corpus_into
from app.db.session import system_session, tenant_session
from app.errors import ProblemError
from app.ingest.catalogue import CATALOGUE
from app.ingest.jobs import (
    IngestAbandoned,
    IngestAlreadyComplete,
    ensure_ingest_admitted,
    mark_ingest_queued,
    read_ingest_status,
    run_ingest,
)
from app.worker.tasks import ingest_source

SOURCE_KEY = "gdpr-full"


async def _count_orgs_with_corpus() -> int:
    async with system_session() as session:
        return await session.scalar(text("SELECT public.count_orgs_with_corpus()"))


_DEFAULT_EXPIRY_SQL = "now() + interval '1 hour'"


async def _make_org(created_orgs: list[str], *, expires_at_sql: str = _DEFAULT_EXPIRY_SQL) -> str:
    """A bare organisation row, no corpus.

    `expires_at_sql` is interpolated directly into the statement rather than
    bound as a parameter — every caller in this module passes a fixed literal
    SQL expression (`now() + interval ...`), never attacker- or test-input
    data, and there is no asyncpg bind form for an interval expression that
    would let it be parameterised instead.
    """
    org_id = str(uuid.uuid4())
    async with tenant_session(org_id) as session:
        await session.execute(
            text(
                "INSERT INTO organizations (id, external_id, name, expires_at) "  # noqa: S608
                f"VALUES (:i, :e, :n, {expires_at_sql})"
            ),
            {"i": org_id, "e": f"ext_{org_id}", "n": "Test org"},
        )
    created_orgs.append(org_id)
    return org_id


async def _add_fake_ingested_chunk(org_id: str, source_key: str = SOURCE_KEY) -> None:
    """One row shaped like ingest's own write, without paying for a real
    parse or embed — everything below that needs "this organisation already
    ingested something" as a precondition uses this instead of a real run."""
    async with tenant_session(org_id) as session:
        await session.execute(
            text(
                "INSERT INTO document_chunks "
                "(id, org_id, content, page_from, page_to, source_key, chunk_ordinal) "
                "VALUES (:i, :o, 'fake ingested chunk', 1, 1, :sk, 0)"
            ),
            {"i": str(uuid.uuid4()), "o": org_id, "sk": source_key},
        )


# --- Step 8's two required tests --------------------------------------------


async def test_a_fresh_demo_org_does_not_count_against_the_ingest_cap(created_orgs) -> None:
    """The seed corpus is not an ingestion, and the cap must not think it is.

    Task 22 gives every seeded chunk a source_key, so the naive predicate
    `source_key IS NOT NULL` counts every organisation that has ever logged
    in — and the cap would then refuse the demo's very first ingestion, at
    the default of 8, after eight logins. Migration 0007 excludes
    `SEED_SOURCE_KEY`; this test is what holds that literal to this constant,
    since nothing else would notice the two drifting apart.

    Measured as a delta against the count taken before seeding, not against
    an assumed 0: the development database is shared and long-lived (see
    `created_orgs`'s own docstring), so an absolute-zero assertion would be
    flaky against whatever another concurrent run happens to hold.
    """
    before = await _count_orgs_with_corpus()
    org_id = await _make_org(created_orgs)
    async with tenant_session(org_id) as session:
        await copy_corpus_into(session, org_id)
    after = await _count_orgs_with_corpus()
    assert after == before


async def test_the_global_cap_refuses_once_the_ceiling_is_held(
    created_orgs, settings_override
) -> None:
    """Sabotage 1 in the task 23 report reverts migration 0007's SECURITY
    DEFINER function to a direct `system_session()` count, which would make
    this test's precondition (`held == before + 1`) already fail — the
    naive query returns 0 regardless of what was just ingested. This test
    therefore fails for the *reverted* code before it ever reaches the
    admission check it exists to prove, which is exactly the property
    Task 23's brief calls out: the obvious implementation both disables the
    ceiling and stays green.
    """
    before = await _count_orgs_with_corpus()
    settings_override(ingest_org_cap=before + 1)

    held_org = await _make_org(created_orgs)
    await _add_fake_ingested_chunk(held_org)
    assert await _count_orgs_with_corpus() == before + 1

    one_more = await _make_org(created_orgs)
    with pytest.raises(ProblemError) as refused:
        await ensure_ingest_admitted(one_more)
    assert refused.value.code == "capacity_unavailable"
    assert refused.value.status == 503


# --- The per-organisation ceiling -------------------------------------------


async def test_ensure_ingest_admitted_refuses_a_second_ingest_for_the_same_org(
    created_orgs,
) -> None:
    org_id = await _make_org(created_orgs)
    await _add_fake_ingested_chunk(org_id)
    with pytest.raises(ProblemError) as refused:
        await ensure_ingest_admitted(org_id)
    assert refused.value.code == "already_ingested"
    assert refused.value.status == 409


async def test_ensure_ingest_admitted_permits_an_org_holding_only_the_seed(
    created_orgs,
) -> None:
    org_id = await _make_org(created_orgs)
    async with tenant_session(org_id) as session:
        await copy_corpus_into(session, org_id)
    await ensure_ingest_admitted(org_id)  # does not raise


# --- The progress hash: written before Redis, read from the rows without it -


async def test_mark_ingest_queued_then_read_ingest_status_round_trip(
    created_orgs, settings_override
) -> None:
    settings_override(redis_url=LIMITS_TEST_REDIS_URL)
    org_id = await _make_org(created_orgs)

    await mark_ingest_queued(org_id)
    status = await read_ingest_status(org_id)

    assert status["stage"] == "queued"
    assert status["chunks_done"] == 0
    assert status["chunks_total"] == 0
    assert status["error"] is None
    assert status["started_at"] is not None


async def test_read_ingest_status_falls_back_to_the_rows_when_the_hash_is_absent(
    created_orgs, settings_override
) -> None:
    settings_override(redis_url=LIMITS_TEST_REDIS_URL)
    org_id = await _make_org(created_orgs)

    idle = await read_ingest_status(org_id)
    assert idle["stage"] == "idle"
    assert idle["chunks_done"] == 0

    await _add_fake_ingested_chunk(org_id)
    complete = await read_ingest_status(org_id)
    assert complete["stage"] == "complete"
    assert complete["chunks_done"] == 1
    assert complete["chunks_total"] == 1


# --- The write-transaction guards, exercised without paying for a real parse
# or embed -----------------------------------------------------------------


async def test_run_ingest_raises_when_the_organisation_has_already_expired(
    created_orgs,
) -> None:
    """The purge and this job share a queue (Step 6); an organisation's TTL
    can lapse before its own ingest task is even picked up. `run_ingest`'s
    fast-path guard catches this before parsing or embedding do any work —
    this test would time out against the full corpus if that guard were
    missing or placed after the expensive stages instead of before them.
    """
    org_id = await _make_org(created_orgs, expires_at_sql="now() - interval '1 second'")
    with pytest.raises(IngestAbandoned):
        await run_ingest(org_id, SOURCE_KEY)


async def test_run_ingest_raises_when_the_organisation_already_has_a_corpus(
    created_orgs,
) -> None:
    """`ensure_ingest_admitted`'s own check runs in the API and can be raced
    by two POSTs before either commits. This is the guard that makes "one
    ingestion per organisation" true rather than likely — repeated inside
    `run_ingest` itself, ahead of the expensive stages, so a duplicate fails
    fast rather than after a wasted embed.
    """
    org_id = await _make_org(created_orgs)
    await _add_fake_ingested_chunk(org_id)
    with pytest.raises(IngestAlreadyComplete):
        await run_ingest(org_id, SOURCE_KEY)


# --- The real thing ----------------------------------------------------------


@pytest.mark.model
@pytest.mark.timeout(180)
async def test_ingest_source_embeds_and_writes_the_full_catalogue_source(
    created_orgs, settings_override
) -> None:
    """The task as a worker actually runs it: `.apply()` off-loop, exactly as
    `test_demo_orgs.py`'s `_run_purge` calls `purge_expired_orgs` — the task
    body calls `asyncio.run()`, which refuses to start inside the loop
    pytest-asyncio already has running.

    Measured at ~75 s wall-clock against the real catalogue PDF, well inside
    `task_soft_time_limit=300`. That figure was taken when the corpus was
    1027 chunks and the parser was pypdf; it is now 978 chunks and a parser
    that costs ~7 s more, so treat 75 s as the right order of magnitude
    rather than a current reading — see the task 23
    report for the full docker-stats run (peak worker memory 452.7 MiB
    against the 650m container limit).
    """
    settings_override(redis_url=LIMITS_TEST_REDIS_URL)
    org_id = await _make_org(created_orgs)

    from app.db.engine import engine

    await engine.dispose()
    result = await asyncio.to_thread(
        lambda: ingest_source.apply(args=(org_id, SOURCE_KEY)).get()
    )

    expected = CATALOGUE[SOURCE_KEY].expected_chunks
    assert result == expected

    async with tenant_session(org_id) as session:
        ordinals = (
            await session.execute(
                text(
                    "SELECT chunk_ordinal FROM document_chunks "
                    "WHERE source_key = :sk ORDER BY chunk_ordinal"
                ),
                {"sk": SOURCE_KEY},
            )
        ).scalars().all()
    assert ordinals == list(range(expected))

    status = await read_ingest_status(org_id)
    assert status["stage"] == "complete"
    assert status["chunks_done"] == expected
    assert status["chunks_total"] == expected

    with pytest.raises(ProblemError) as refused:
        await ensure_ingest_admitted(org_id)
    assert refused.value.code == "already_ingested"
