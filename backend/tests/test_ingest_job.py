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

from app.auth.demo import DEMO_TOKEN_TTL_SECONDS
from app.chat.corpus import copy_corpus_into
from app.db.session import system_session, tenant_session
from app.errors import ProblemError
from app.ingest.catalogue import CATALOGUE
from app.ingest.jobs import (
    _INGEST_ORG_TTL_SECONDS,
    _RELEASE_SLOT_SQL,
    IngestAbandoned,
    IngestAlreadyComplete,
    _guard_write_transaction,
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
    result = await asyncio.to_thread(lambda: ingest_source.apply(args=(org_id, SOURCE_KEY)).get())

    expected = CATALOGUE[SOURCE_KEY].expected_chunks
    assert result == expected

    async with tenant_session(org_id) as session:
        ordinals = (
            (
                await session.execute(
                    text(
                        "SELECT chunk_ordinal FROM document_chunks "
                        "WHERE source_key = :sk ORDER BY chunk_ordinal"
                    ),
                    {"sk": SOURCE_KEY},
                )
            )
            .scalars()
            .all()
        )
    assert ordinals == list(range(expected))

    status = await read_ingest_status(org_id)
    assert status["stage"] == "complete"
    assert status["chunks_done"] == expected
    assert status["chunks_total"] == expected

    with pytest.raises(ProblemError) as refused:
        await ensure_ingest_admitted(org_id)
    assert refused.value.code == "already_ingested"


# --- The purge window `_ALIVE_SQL`'s row lock closes -------------------------
#
# Migration 0004's residual has been re-derived four times, and every previous
# derivation ended at "a narrowed residual, not a closed window": the guard was
# a plain `SELECT EXISTS`, so an ingest whose write transaction committed
# strictly between `purge_expired_orgs`'s two statements left chunks that
# statement 2's fresh snapshot had already passed. Those rows are unreachable
# forever — RLS keys on an org_id no `organizations` row carries — and they
# counted against `INGEST_ORG_CAP`.
#
# `FOR KEY SHARE` closes it, and these two tests are the evidence rather than
# the argument.


async def _purge_as_worker(sessions) -> int:
    # `tenant_scoped=False`, the same declaration `system_session()` makes: the
    # purge is unscoped by definition, and `_require_tenant_context` raises for
    # any session that does not say so.
    async with sessions(info={"tenant_scoped": False}) as session:
        purged = await session.scalar(text("SELECT public.purge_expired_orgs()"))
        await session.commit()
        return purged


async def test_the_liveness_guard_can_take_its_row_lock_as_the_worker_role(
    created_orgs, worker_role
) -> None:
    """A locking clause needs UPDATE on the table, and the whole point of
    `worker_role` is that `lexloom_worker` holds a different grant set from
    `lexloom_app` (migration 0004). `run_ingest` runs as the worker, so a
    `FOR KEY SHARE` the worker may not take would be a permission error in
    production and nowhere else.
    """
    org_id = await _make_org(created_orgs)
    async with tenant_session(org_id) as session:
        await _guard_write_transaction(session, org_id)  # does not raise


async def test_the_write_transaction_blocks_the_purge_rather_than_racing_it(
    created_orgs,
) -> None:
    """The measurement, not the reasoning.

    An organisation is given two seconds to live and its row lock is taken
    while it is still alive — which is exactly the state a real job is in when
    it finishes embedding. The TTL then lapses *inside* the write transaction,
    the purge runs, and the two assertions are the two halves of the claim:
    the purge is still blocked while the ingest holds the lock, and once the
    ingest commits the purge collects the chunks it wrote rather than leaving
    them orphaned.

    Sabotage: drop `FOR KEY SHARE` from `_ALIVE_SQL` and the first assertion
    fails immediately — the purge sails through — and the second fails after
    it, with the chunk still present and its organisation gone.

    The purge runs on its own engine as `lexloom_worker` rather than through
    the `worker_role` fixture, because that fixture's engine is `pool_size=1`
    and this test needs two connections open at once.
    """
    from conftest import WORKER_DATABASE_URL  # noqa: PLC2701
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    org_id = await _make_org(created_orgs, expires_at_sql="now() + interval '2 seconds'")
    worker_engine = create_async_engine(WORKER_DATABASE_URL, pool_size=2, max_overflow=0)
    sessions = async_sessionmaker(worker_engine, expire_on_commit=False)

    try:
        async with tenant_session(org_id) as session:
            await _guard_write_transaction(session, org_id)  # alive: the lock is taken

            # Past the TTL, still inside the transaction that holds the lock.
            await asyncio.sleep(2.5)
            purge = asyncio.create_task(_purge_as_worker(sessions))
            await asyncio.sleep(1.0)
            assert not purge.done(), (
                "the purge deleted the organisation while an ingest transaction held "
                "its row lock — FOR KEY SHARE is not being taken"
            )

            await session.execute(
                text(
                    "INSERT INTO document_chunks "
                    "(id, org_id, content, page_from, page_to, source_key, chunk_ordinal) "
                    "VALUES (:i, :o, 'written under the lock', 1, 1, :sk, 0)"
                ),
                {"i": str(uuid.uuid4()), "o": org_id, "sk": SOURCE_KEY},
            )

        assert await purge >= 1, "the purge did not collect the expired organisation"

        async with tenant_session(org_id) as session:
            left_behind = await session.scalar(
                text("SELECT count(*) FROM document_chunks WHERE org_id = :o"), {"o": org_id}
            )
        assert left_behind == 0, (
            f"{left_behind} chunk(s) survived the purge of their own organisation — "
            "the residual window is open"
        )
    finally:
        await worker_engine.dispose()


# --- The reserved slot: I1's probe, and what fell out of it -------------------
#
# The finding that produced these: `ensure_ingest_admitted` read
# `count_orgs_with_corpus()`, which counts *committed* corpora, and nothing
# commits during admission — so with two free slots, twelve of twelve
# consecutive admissions passed and the whole burst then committed.
# `_guard_write_transaction` re-checks liveness and the per-organisation
# duplicate but never the global cap, so there was no second place to refuse.


async def _reservation_of(org_id: str):
    """This organisation's `ingest_reserved_at`, read in its own tenant context.

    Per organisation rather than as a `count(*)` across the table, because
    there is no session that could legitimately count reservations across every
    tenant: `system_session()` sets no GUC, and `organizations`' RLS policy
    (migration 0003) then matches zero rows. That is the same constraint
    migration 0007 exists for, and the reason the accounting itself lives in a
    SECURITY DEFINER function.
    """
    async with tenant_session(org_id) as session:
        return await session.scalar(
            text("SELECT ingest_reserved_at FROM organizations WHERE id = :o"), {"o": org_id}
        )


async def _admit(org_id: str) -> str | None:
    """Admit, and report the refusal code instead of raising."""
    try:
        await ensure_ingest_admitted(org_id)
    except ProblemError as refused:
        return refused.code
    return None


async def test_a_burst_of_concurrent_admissions_takes_exactly_the_free_slots(
    created_orgs, settings_override
) -> None:
    """The reviewer's probe, as a test: twelve concurrent admissions against a
    cap with two free slots.

    Before the reservation existed this admitted twelve of twelve — not
    "sometimes raced", but every time, because the value being read
    (`count_orgs_with_corpus()`) could not move until a job finished, and no
    job finishes during a POST. The cost of the ten extra was never the ~1.2 MB
    per tenant the old docstring priced it at: it was ten queued jobs at ~75 s
    of pinned CPU each, on two vCPUs shared with someone else's production,
    with `purge_expired_orgs` behind them on the same queue.

    Sabotage: revert `ensure_ingest_admitted` to counting committed corpora and
    the first assertion reports 12.

    A cross-check on the rows follows the count, because "two were admitted" and
    "two hold a slot" are different claims and only the second one is the
    mechanism. If this fails with 0 or 1 admitted rather than 2, the shared
    development database is holding live ingest reservations from another run —
    the baseline below counts committed corpora, which those do not appear in.
    """
    burst = [await _make_org(created_orgs) for _ in range(12)]
    async with system_session() as session:
        committed = await session.scalar(text("SELECT public.count_orgs_with_corpus()"))
    settings_override(ingest_org_cap=committed + 2)

    outcomes = await asyncio.gather(*(_admit(org_id) for org_id in burst))

    admitted = [org for org, code in zip(burst, outcomes, strict=True) if code is None]
    assert len(admitted) == 2, (
        f"{len(admitted)} of {len(burst)} admissions passed a cap with two free slots"
    )
    assert all(code == "capacity_unavailable" for code in outcomes if code is not None)

    reserved = [org for org in burst if await _reservation_of(org) is not None]
    assert reserved == admitted, "the organisations admitted are not the ones holding a slot"


async def test_an_organisation_that_already_holds_a_slot_is_readmitted_when_the_cap_is_full(
    created_orgs, settings_override
) -> None:
    """A failed job leaves its reservation behind — `mark_ingest_queued` says a
    retry is permitted, and a retry refused by a ceiling the retrying tenant is
    itself inside would be the ceiling counting one tenant twice."""
    async with system_session() as session:
        committed = await session.scalar(text("SELECT public.count_orgs_with_corpus()"))
    settings_override(ingest_org_cap=committed + 1)

    org_id = await _make_org(created_orgs)
    assert await _admit(org_id) is None  # takes the last slot

    other = await _make_org(created_orgs)
    assert await _admit(other) == "capacity_unavailable"  # the cap is full

    assert await _admit(org_id) is None, "a retry was refused by its own reservation"


async def test_a_committed_ingest_hands_its_reservation_over_to_its_corpus(
    created_orgs, settings_override
) -> None:
    """The two terms `reserve_ingest_slot` adds together must not overlap, or a
    tenant that finished an ingest would occupy two slots for an hour."""
    async with system_session() as session:
        committed = await session.scalar(text("SELECT public.count_orgs_with_corpus()"))
    settings_override(ingest_org_cap=committed + 5)

    org_id = await _make_org(created_orgs)
    await ensure_ingest_admitted(org_id)
    assert await _reservation_of(org_id) is not None

    # The write transaction's own two statements, without the parse and the
    # embed: `run_ingest`'s INSERT is covered by the round-trip test above, and
    # what is under test here is that the release travels with it.
    async with tenant_session(org_id) as session:
        await _guard_write_transaction(session, org_id)
        await session.execute(
            text(
                "INSERT INTO document_chunks "
                "(id, org_id, content, page_from, page_to, source_key, chunk_ordinal) "
                "VALUES (:i, :o, 'committed', 1, 1, :sk, 0)"
            ),
            {"i": str(uuid.uuid4()), "o": org_id, "sk": SOURCE_KEY},
        )
        await session.execute(_RELEASE_SLOT_SQL, {"org": org_id})

    assert await _reservation_of(org_id) is None
    async with system_session() as session:
        assert await session.scalar(text("SELECT public.count_orgs_with_corpus()")) == committed + 1


async def test_an_orphaned_corpus_does_not_consume_a_slot(created_orgs) -> None:
    """Migration 0007's count never joined `organizations`, so chunks whose
    organisation was gone held a slot with no way to release it — the cap
    ratcheting down permanently, which is the one-way latch Task 23 Step 5
    exists to prevent. `FOR KEY SHARE` closes the last way to produce one; this
    is what makes the ones already in a long-lived database harmless.
    """
    org_id = await _make_org(created_orgs)
    await _add_fake_ingested_chunk(org_id)
    async with system_session() as session:
        with_org = await session.scalar(text("SELECT public.count_orgs_with_corpus()"))

    # Orphan it: the organisation goes, its chunks stay. Exactly the state the
    # old residual produced, reached deliberately rather than by racing.
    async with tenant_session(org_id) as session:
        await session.execute(text("DELETE FROM organizations WHERE id = :o"), {"o": org_id})

    async with system_session() as session:
        without_org = await session.scalar(text("SELECT public.count_orgs_with_corpus()"))
    assert without_org == with_org - 1


async def test_an_expired_organisation_stops_holding_a_slot_before_the_purge_runs(
    created_orgs,
) -> None:
    """The purge fires hourly, so counting expired tenants would refuse a
    visitor for up to an hour over rows that are already condemned. The
    overshoot this admits is bounded by the cap itself — ~1.1 MB per tenant, so
    ~10 MB against a 384 MB Postgres — and it is what makes the shortened TTL
    below actually give capacity back when it lapses.
    """
    org_id = await _make_org(created_orgs, expires_at_sql="now() - interval '1 minute'")
    await _add_fake_ingested_chunk(org_id)
    async with system_session() as session:
        held = await session.scalar(text("SELECT public.count_orgs_with_corpus()"))

    live_org = await _make_org(created_orgs)
    await _add_fake_ingested_chunk(live_org)
    async with system_session() as session:
        assert await session.scalar(text("SELECT public.count_orgs_with_corpus()")) == held + 1


async def test_admission_shortens_an_ingesting_organisations_ttl(
    created_orgs, settings_override
) -> None:
    """I2's trade-off, as a measurement. Eight logins plus eight ingests filled
    the cap for the full 24-hour demo TTL, at negligible cost to whoever did it;
    two hours instead means it has to be re-mounted rather than done once.
    """
    async with system_session() as session:
        committed = await session.scalar(text("SELECT public.count_orgs_with_corpus()"))
    settings_override(ingest_org_cap=committed + 5)

    org_id = await _make_org(created_orgs, expires_at_sql="now() + interval '24 hours'")
    await ensure_ingest_admitted(org_id)

    async with tenant_session(org_id) as session:
        remaining = await session.scalar(
            text("SELECT expires_at - now() FROM organizations WHERE id = :o"), {"o": org_id}
        )
    assert remaining.total_seconds() <= _INGEST_ORG_TTL_SECONDS
    # And not shortened past the token that is already out there.
    assert remaining.total_seconds() > DEMO_TOKEN_TTL_SECONDS


async def test_admission_never_extends_an_organisations_ttl(
    created_orgs, settings_override
) -> None:
    """`LEAST`, not an assignment. A tenant that already expires sooner than the
    ingest TTL must not be handed extra life by starting an ingest — and neither
    must one with no expiry at all, which is the seed-corpus organisation.
    """
    async with system_session() as session:
        committed = await session.scalar(text("SELECT public.count_orgs_with_corpus()"))
    settings_override(ingest_org_cap=committed + 5)

    short = await _make_org(created_orgs, expires_at_sql="now() + interval '5 minutes'")
    await ensure_ingest_admitted(short)
    async with tenant_session(short) as session:
        remaining = await session.scalar(
            text("SELECT expires_at - now() FROM organizations WHERE id = :o"), {"o": short}
        )
    assert remaining.total_seconds() <= 300

    permanent = await _make_org(created_orgs, expires_at_sql="NULL")
    await ensure_ingest_admitted(permanent)
    async with tenant_session(permanent) as session:
        expires_at = await session.scalar(
            text("SELECT expires_at FROM organizations WHERE id = :o"), {"o": permanent}
        )
    assert expires_at is None


def test_an_ingesting_org_still_outlives_every_token_for_it() -> None:
    """Migration 0004's residual argument for API-path child rows rests on no
    live token existing when an organisation expires. Shortening an ingesting
    tenant's TTL is the one change in this milestone that could break it, so the
    two numbers are held to each other here rather than left to be re-derived.

    The margin is real and not merely arithmetic: a token is minted at login and
    this TTL starts at admission, which is strictly later, so the token's
    deadline is always the earlier of the two by at least the gap between them.
    """
    assert _INGEST_ORG_TTL_SECONDS > DEMO_TOKEN_TTL_SECONDS


# --- Migration 0010: a tenant is counted once, and is never refused a ceiling
#     it is already inside --------------------------------------------------


async def _reserve(org_id: str, cap: int) -> bool:
    """Call the admission function directly, as the API role does."""
    async with tenant_session(org_id) as session:
        return bool(
            await session.scalar(
                text("SELECT public.reserve_ingest_slot(:cap, 3600, 7200)"),
                {"cap": cap},
            )
        )


async def test_a_tenant_holding_a_corpus_and_a_reservation_is_counted_once(
    created_orgs,
) -> None:
    """0008's sum rests on a precondition per-source ingest retires.

    Its own comment states it: "a tenant holds a reservation or a corpus, never
    both". Once a tenant can ingest a second document, it holds both for the
    duration of that job and is added to the ceiling twice — so an unrelated
    tenant is refused while a slot is genuinely free.

    The state is built by hand rather than by running two ingests, because the
    code that produces it naturally arrives in the next task. That is the point
    of putting this migration first: the fix lands before the feature that
    triggers the bug.

    The assertion is behavioural — a neighbour's admission — rather than a
    recomputation of the sum. Asserting the arithmetic would only test the SQL
    this test itself wrote; asserting the neighbour tests the function.
    """
    ingesting = await _make_org(created_orgs)
    await _add_fake_ingested_chunk(ingesting)

    # It holds a corpus; admitting it for a second document gives it a
    # reservation too, which is the state 0008 declared impossible.
    assert await _reserve(ingesting, cap=1000) is True

    # One slot free, counting the tenants that actually hold a corpus.
    cap = await _count_orgs_with_corpus() + 1
    neighbour = await _make_org(created_orgs)
    assert await _reserve(neighbour, cap=cap) is True, (
        "an unrelated tenant was refused while a slot was free: the ingesting "
        "tenant is being counted by both terms of the admission sum"
    )


async def test_a_tenant_inside_a_full_ceiling_is_still_admitted_for_a_second_source(
    created_orgs,
) -> None:
    """The half 0008's short-circuit could not see.

    `v_holds_slot` asked only about a live reservation, so a tenant holding a
    corpus and no reservation fell through to the cap check — where the corpus
    term counts it. With the cap full it was refused a slot it already occupies.
    """
    org_id = await _make_org(created_orgs)
    await _add_fake_ingested_chunk(org_id)
    held = await _count_orgs_with_corpus()

    # A cap already fully consumed by the tenants that hold a corpus.
    assert await _reserve(org_id, cap=held) is True


async def test_the_admission_function_is_still_executable_by_the_api_role() -> None:
    """0010 replaces the function in place precisely so 0008's grants survive.

    A changed signature would be a new function: PUBLIC would get EXECUTE by
    default, `lexloom_app` would get none, and `EXPECTED_RELATIONS` would not
    notice because it keys on name without an argument dimension.
    """
    async with system_session() as session:
        allowed = await session.scalar(
            text(
                "SELECT has_function_privilege('lexloom_app', "
                "'public.reserve_ingest_slot(integer, integer, integer)', 'EXECUTE')"
            )
        )
    assert allowed is True
