import asyncio
import contextlib
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, ProgrammingError

from app.auth.demo import DEMO_ORG_TTL, DEMO_TOKEN_TTL_SECONDS
from app.db.session import system_session, tenant_session
from app.worker.tasks import is_deadlock, purge_expired_orgs

# The tables this module seeds and then proves empty. A literal on purpose —
# `test_the_purge_covers_every_org_scoped_table` compares it against the
# catalog, so it cannot quietly fall behind the schema.
CHILD_COUNT_SQL = {
    "conversations": "SELECT count(*) FROM conversations WHERE org_id = :org",
    "messages": "SELECT count(*) FROM messages WHERE org_id = :org",
    "document_chunks": "SELECT count(*) FROM document_chunks WHERE org_id = :org",
}

# Both settings pinned by value, not by presence. `SET search_path = public,
# pg_catalog` is a valid pin and a catastrophic one: listing pg_catalog last
# suppresses its implicit-first placement, so a hostile `public.now()` decides
# which tenants are expired and the purge deletes live ones.
EXPECTED_PURGE_FUNCTION_SETTINGS = {"search_path=pg_catalog, public", "row_security=off"}

# --- The catalog inventory ---------------------------------------------------
#
# Four rounds of review broke a guard that asked "is there a reachable object
# that could leak an organisation id?". Answering that means enumerating
# privilege routes, and the enumeration was wrong five times, each time in a
# different corner of Postgres privilege semantics: `has_function_privilege`
# folds in inherited membership only, so `GRANT r TO app WITH INHERIT FALSE, SET
# TRUE` was invisible; it also misses MEMBER-only grants; EXECUTE on a trigger
# function is checked at CREATE TRIGGER time rather than at fire time, so a
# SECURITY DEFINER trigger function with no grant at all fires on an ordinary
# INSERT; `has_table_privilege(…, 'SELECT')` is false when the grant is
# `GRANT SELECT (id)` on one column, which is the *more* idiomatic way to expose
# just an id; and the pg_catalog/information_schema carve-out was defended by
# asserting the application roles cannot CREATE there, while the objects it
# existed to catch are created by the owner, who can.
#
# The threat model those probes shared is not an attacker — an attacker holding
# owner rights already has everything. It is a careless future migration, run as
# `lexloom_migrator`. The space of ways a migration can accidentally expose data
# is unbounded, so enumerating reachability cannot converge.
#
# So the question is inverted. Not "what is reachable" but "what exists".
# Postgres gives an exact discriminator: `FirstNormalObjectId` is 16384, and
# every OID below it was handed out during initdb. `oid >= 16384` therefore
# splits what shipped with the database from what a migration put there, with no
# privilege semantics involved at all, in any Postgres version. An inventory
# assertion over that set catches the trigger function, the column-granted view,
# the materialised view, the object hidden in `information_schema` and the
# shapes nobody has thought of yet, in one assertion — and it gets *smaller* as
# it gets stronger rather than larger.
#
# TOAST relations are reported by their parent rather than their own name: a
# TOAST table is called `pg_toast_18084`, and that number is an OID, so it
# differs between this workstation and CI and cannot be written as a literal.
# Nothing is lost by the substitution — a TOAST relation exists only as an
# attachment to a heap relation, and that parent is itself in the inventory
# below, named.
USER_CREATED_RELATIONS_SQL = """
SELECT n.nspname AS schema_name,
       coalesce(toast.parent, toast_index.parent, c.relname) AS relation_name,
       c.relkind::pg_catalog.text AS relation_kind
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN LATERAL (
    SELECT 'TOAST of ' || pn.nspname || '.' || p.relname AS parent
    FROM pg_catalog.pg_class p
    JOIN pg_catalog.pg_namespace pn ON pn.oid = p.relnamespace
    WHERE p.reltoastrelid = c.oid
) AS toast ON true
LEFT JOIN LATERAL (
    SELECT 'TOAST index of ' || pn.nspname || '.' || p.relname AS parent
    FROM pg_catalog.pg_index i
    JOIN pg_catalog.pg_class t ON t.oid = i.indrelid
    JOIN pg_catalog.pg_class p ON p.reltoastrelid = t.oid
    JOIN pg_catalog.pg_namespace pn ON pn.oid = p.relnamespace
    WHERE i.indexrelid = c.oid
) AS toast_index ON true
WHERE c.oid >= 16384  -- FirstNormalObjectId: everything below shipped with initdb
"""

# Every relation migrations 0001-0004 create, and nothing else. Indexes are in
# here as well as tables: an index cannot leak a row by itself, but leaving a
# relkind out would reintroduce exactly the carve-out this inventory exists to
# remove, and the cost of keeping it is one line per index.
EXPECTED_RELATIONS = {
    ("public", "organizations", "r"),
    ("public", "conversations", "r"),
    ("public", "messages", "r"),
    ("public", "document_chunks", "r"),
    ("public", "alembic_version", "r"),
    ("public", "organizations_pkey", "i"),
    ("public", "organizations_external_id_key", "i"),
    ("public", "ix_organizations_expires_at", "i"),
    ("public", "conversations_pkey", "i"),
    ("public", "ix_conversations_org_created", "i"),
    ("public", "messages_pkey", "i"),
    ("public", "ix_messages_org_conversation", "i"),
    ("public", "document_chunks_pkey", "i"),
    ("public", "ix_document_chunks_org", "i"),
    ("public", "alembic_version_pkc", "i"),
    ("pg_toast", "TOAST of public.organizations", "t"),
    ("pg_toast", "TOAST of public.messages", "t"),
    ("pg_toast", "TOAST of public.document_chunks", "t"),
    ("pg_toast", "TOAST index of public.organizations", "i"),
    ("pg_toast", "TOAST index of public.messages", "i"),
    ("pg_toast", "TOAST index of public.document_chunks", "i"),
}

# The same inventory for `pg_proc`, which holds functions, procedures,
# aggregates and window functions alike.
#
# Two things are deliberately narrower here than in the relation inventory, and
# both are stated so the check is as wide as the claim.
#
# Extension members are skipped *unless* they are SECURITY DEFINER. `vector`
# alone contributes 118 functions; listing them would put an inventory nobody
# can maintain in front of every extension upgrade, and an inventory people
# routinely paste over is not a guard. The exclusion is bounded by the
# extension inventory asserted alongside it, and `ALTER EXTENSION … ADD
# FUNCTION` cannot be used to smuggle a definer function past it because the
# `p.prosecdef` disjunct admits definer functions regardless of membership.
#
# `prosecdef` is carried in the key because it is the property that decides
# whether a function can see past row-level security at all: a function that is
# not SECURITY DEFINER executes with the caller's privileges, so the policies in
# migrations 0002 and 0003 bind its statements exactly as they bind the caller's
# own. That is why the discriminator is worth recording, not a reason to filter
# on it — every user-created non-extension function is listed either way.
USER_CREATED_FUNCTIONS_SQL = """
SELECT n.nspname AS schema_name,
       p.proname AS function_name,
       pg_catalog.pg_get_function_identity_arguments(p.oid) AS arguments,
       p.prokind::pg_catalog.text AS kind,
       p.prosecdef AS security_definer
FROM pg_catalog.pg_proc p
JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
WHERE p.oid >= 16384  -- FirstNormalObjectId, as above
  AND (
        p.prosecdef
     OR NOT EXISTS (
            SELECT 1
            FROM pg_catalog.pg_depend d
            WHERE d.classid = 'pg_catalog.pg_proc'::pg_catalog.regclass
              AND d.objid = p.oid
              AND d.deptype = 'e'
        )
      )
"""

# (schema, name, identity arguments, prokind, prosecdef). Argument-less and
# `f` (a plain function, not an aggregate or a procedure) are part of the vetted
# shape, not incidental: a variant taking an argument could be steered.
EXPECTED_FUNCTIONS = {("public", "purge_expired_orgs", "", "f", True)}

# What bounds the extension carve-out above. `plpgsql` is created by initdb;
# `vector` is what the pgvector image installs and what migration 0001 needs.
EXPECTED_EXTENSIONS = {"plpgsql", "vector"}

PURGE_FUNCTION_SQL = """
SELECT prosecdef, proconfig, has_function_privilege('public', oid, 'EXECUTE') AS public_executes
FROM pg_proc WHERE oid = 'public.purge_expired_orgs()'::regprocedure
"""


async def _run_purge() -> int:
    """Invoke the Celery task the way a worker does: synchronously, off-loop.

    The task calls asyncio.run, which refuses to start inside an event loop
    that is already running — and pytest-asyncio is running one. A worker
    thread has no loop of its own, so it is the only place `.apply()` behaves
    here the way it behaves in the worker process.

    The engine is disposed on this side first. Its pool is a module global; a
    connection opened on the test's loop and handed to the thread's loop fails
    in ways that point at the wrong code entirely, which is the same reason the
    task disposes it again before its own loop closes.
    """
    from app.db.engine import engine

    await engine.dispose()
    return await asyncio.to_thread(lambda: purge_expired_orgs.apply().get())


# Bounds for the connections the concurrency test opens. That test deliberately
# makes the purge block, so these are generous rather than tight — what they rule
# out is the case where it never unblocks. Without them a genuine block is a hang,
# and a hung pytest on GitHub Actions runs to the six-hour job default.
# `lock_timeout` is the one that matters (the purge is waiting on a row lock);
# `statement_timeout` catches a wait that is not a lock wait.
CONCURRENCY_TEST_TIMEOUTS = {"lock_timeout": "60s", "statement_timeout": "90s"}

# How long to wait for the purge backend to actually reach the row lock before
# declaring the race unreproduced. Generous on purpose: it is a bound, not a
# budget — the poll returns as soon as the block is visible, so a fast machine
# pays none of it and a loaded 2-vCPU CI runner is not punished for being slow.
BLOCK_OBSERVATION_TIMEOUT = 30.0

# The purge backend, seen from another connection, while it waits. `Lock` is the
# wait_event_type Postgres reports for a row lock held by another transaction.
# `pg_stat_activity` shows `query` and `wait_event_type` for another backend only
# to a superuser, a member of pg_read_all_stats, or the same role — which is why
# the observer connects as lexloom_worker, exactly as the purge does.
BLOCKED_PURGE_BACKEND_SQL = """
SELECT count(*)
FROM pg_catalog.pg_stat_activity
WHERE pid <> pg_catalog.pg_backend_pid()
  AND wait_event_type = 'Lock'
  AND query LIKE '%purge_expired_orgs%'
"""


def _worker_engine():
    """A single-connection engine as lexloom_worker, with the timeouts above.

    asyncpg sends `server_settings` in the startup packet, so the bounds are in
    force from the connection's first statement rather than from whenever a
    `SET` would have run.
    """
    from conftest import WORKER_DATABASE_URL
    from sqlalchemy.ext.asyncio import create_async_engine

    return create_async_engine(
        WORKER_DATABASE_URL,
        pool_size=1,
        max_overflow=0,
        connect_args={"server_settings": dict(CONCURRENCY_TEST_TIMEOUTS)},
    )


def _purge_in_a_new_loop() -> int:
    """Call the SQL function on its own connection, from a thread with no loop.

    Used only by the concurrency test, which has to hold one transaction open on
    the test's loop while another blocks inside the database. A separate engine
    keeps the two off the same pooled connection, which would otherwise
    deadlock the test rather than the purge.
    """
    import asyncio as _asyncio

    async def _run() -> int:
        engine = _worker_engine()
        try:
            async with engine.begin() as conn:
                return await conn.scalar(text("SELECT public.purge_expired_orgs()"))
        finally:
            await engine.dispose()

    return _asyncio.run(_run())


async def _wait_until_the_purge_blocks_on_a_lock(observer) -> None:
    """Return once the purge backend is provably waiting on a lock; fail if it never is.

    This replaced `await asyncio.sleep(0.4)`. That sleep was the only thing
    making the race happen, and nothing in the run distinguished "raced and
    passed" from "never raced": with `sleep(0)` the single-statement purge body
    this test exists to reject passed three times out of three. The 400 ms had
    to cover a thread start, a fresh async engine, a TCP connect, a SCRAM
    handshake and the row lock — 64-96 ms measured on an idle workstation, which
    is comfortable there and is not a margin anyone should rely on for a 2-vCPU
    CI runner talking to a service container.

    So the wait is now an observation. A test that cannot confirm it reproduced
    the race must fail rather than pass quietly.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + BLOCK_OBSERVATION_TIMEOUT
    while loop.time() < deadline:
        async with observer.connect() as conn:
            if await conn.scalar(text(BLOCKED_PURGE_BACKEND_SQL)):
                return
        await asyncio.sleep(0.02)
    raise AssertionError(
        "the purge never reached the row lock within "
        f"{BLOCK_OBSERVATION_TIMEOUT:.0f}s, so the race this test exists to reproduce did "
        "not happen and a green result would mean nothing. Either the purge failed before "
        "reaching the DELETE, or it is no longer taking the organisations row lock first"
    )

async def _seed_org(created_orgs: list[str], *, expires_at: datetime | None) -> str:
    """Create one organisation owning one row in every org-scoped table.

    A purge that drops the organisation row and nothing else looks correct from
    the organisations table alone, so every assertion about the purge has to be
    able to look at the children too.
    """
    org_id = str(uuid.uuid4())
    async with system_session() as session:
        await session.execute(
            text(
                "INSERT INTO organizations (id, external_id, name, expires_at) "
                "VALUES (:i, :e, :n, :x)"
            ),
            {"i": org_id, "e": f"seed_{org_id}", "n": "Seeded Tenant", "x": expires_at},
        )
    created_orgs.append(org_id)

    conversation_id = str(uuid.uuid4())
    async with tenant_session(org_id) as session:
        await session.execute(
            text("INSERT INTO conversations (id, org_id, title) VALUES (:i, :o, 'seeded')"),
            {"i": conversation_id, "o": org_id},
        )
        await session.execute(
            text(
                "INSERT INTO messages (id, org_id, conversation_id, role, content) "
                "VALUES (:i, :o, :c, 'user', 'seeded')"
            ),
            {"i": str(uuid.uuid4()), "o": org_id, "c": conversation_id},
        )
        await session.execute(
            text(
                "INSERT INTO document_chunks (id, org_id, content, page_from, page_to) "
                "VALUES (:i, :o, 'seeded', 1, 1)"
            ),
            {"i": str(uuid.uuid4()), "o": org_id},
        )
    return org_id


async def test_two_demo_logins_get_different_organisations(
    client: AsyncClient, created_orgs: list[str]
) -> None:
    first = (
        await client.post("/api/v1/auth/demo-login", json={"label": "Acme Legal"})
    ).json()
    second = (
        await client.post("/api/v1/auth/demo-login", json={"label": "Acme Legal"})
    ).json()
    created_orgs.extend([first["org_id"], second["org_id"]])
    assert first["org_id"] != second["org_id"]


async def test_demo_org_carries_an_expiry(
    client: AsyncClient, created_orgs: list[str]
) -> None:
    """Read through the new organisation's own tenant_session, not an unscoped one.

    organizations' SELECT policy is `id = current_setting('app.current_org_id')`,
    so an unscoped session matches zero rows and a successful creation is
    indistinguishable from a failed one. Asserting `expires_at is not None`
    against a query that structurally returns nothing asserts nothing.
    """
    body = (await client.post("/api/v1/auth/demo-login", json={"label": "Globex"})).json()
    created_orgs.append(body["org_id"])
    async with tenant_session(body["org_id"]) as session:
        expires_at = await session.scalar(
            text("SELECT expires_at FROM organizations WHERE id = :i"), {"i": body["org_id"]}
        )
    assert expires_at is not None
    # Bounded on both sides: a regression that stamped `now()` would satisfy
    # "not None" while leaving every demo tenant already expired at birth.
    assert datetime.now(UTC) < expires_at <= datetime.now(UTC) + DEMO_ORG_TTL


async def test_demo_login_reports_the_lifetime_of_the_token_it_actually_issued(
    client: AsyncClient, created_orgs: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`expires_in` is a promise about the token, so derive it from the token.

    Patched to a value `DevIdp.mint` does not default to. With the constant left
    at its real 3600 — identical to mint()'s default — deleting `ttl=` from the
    call site would leave both assertions below green while the response stopped
    describing the token at all.
    """
    monkeypatch.setattr("app.api.v1.auth.DEMO_TOKEN_TTL_SECONDS", 1234)
    body = (await client.post("/api/v1/auth/demo-login", json={"label": "Initech"})).json()
    created_orgs.append(body["org_id"])
    claims = jwt.decode(
        body["access_token"], options={"verify_signature": False}, audience="lexloom-api"
    )
    assert body["expires_in"] == 1234
    assert claims["exp"] - claims["iat"] == 1234
    assert claims["org_id"] == body["org_id"]


async def test_demo_login_rejects_a_label_with_nothing_visible_in_it(
    client: AsyncClient, created_orgs: list[str]
) -> None:
    """An organisation named "" is indistinguishable in the UI from one whose
    name failed to load, and the row is unbounded-creation fuel either way.

    Five input classes, each of which defeated an earlier version of the check:
    plain whitespace; U+200B ZERO WIDTH SPACE, which is not `str.isspace()` and
    so survives `strip_whitespace`; the Hangul and Braille fillers, which are
    ordinary `Lo`/`So` characters that happen to draw nothing and therefore
    cannot be excluded by any category rule; bare combining marks, which have
    no base character to attach to; and the unassigned (`Cn`) invisibles, which
    passed every earlier version of this check because `Cn` was not in the
    rejected set — reserved Default_Ignorable codepoints and noncharacters, each
    of which created a real organisation with a one-character invisible name.
    U+00A0 NO-BREAK SPACE is here for the opposite reason — Python strips it,
    Postgres would have stored it — so a future switch to a hand-rolled
    `.strip(" ")` fails loudly.

    The validator is not a rendering oracle and does not claim to be; see its
    docstring. This list is the known ways to produce a blank name, not all of
    them.
    """
    # Written as escapes on purpose: a literal zero-width space in source is
    # invisible to the next reader and survives a careless reformat unnoticed.
    blank_labels = (
        "",
        "   ",
        "\u200b",  # ZERO WIDTH SPACE (Cf) — not str.isspace()
        "\u00a0",  # NO-BREAK SPACE (Zs) — Python strips it, Postgres would not
        "\u200b\u00a0\u200b",
        "\u3164",  # HANGUL FILLER (Lo) — the canonical blank name
        "\u115f",  # HANGUL CHOSEONG FILLER (Lo)
        "\u1160",  # HANGUL JUNGSEONG FILLER (Lo)
        "\uffa0",  # HALFWIDTH HANGUL FILLER (Lo)
        "\u2800",  # BRAILLE PATTERN BLANK (So)
        "\u0301",  # COMBINING ACUTE ACCENT (Mn) — no base character to sit on
        "\u034f",  # COMBINING GRAPHEME JOINER (Mn)
        "\u17b4",  # KHMER VOWEL INHERENT AQ (Mn)
        "\u3164\u2800\u0301",
        # Unassigned (Cn). Every one of these was accepted before `Cn` joined
        # the rejected categories, each creating a real organisation whose name
        # was one invisible character long: verified over HTTP, 200 with
        # len(name) == 1.
        "\u2065",  # reserved inside the Default_Ignorable run at U+2060
        "\ufff0",  # reserved, Default_Ignorable
        "\ufff8",  # reserved, Default_Ignorable
        "\U000e0002",  # reserved in the tag block, Default_Ignorable
        "\U000e0080",  # reserved in the variation-selector supplement
        "\ufdd0",  # noncharacter: unassigned permanently, by design
        "\u2065\ufdd0\U000e0002",
    )
    for label in blank_labels:
        response = await client.post("/api/v1/auth/demo-login", json={"label": label})
        if response.status_code == 200:
            # Registered before the assertion fires: a regression here should
            # fail loudly without also leaving behind the row it should never
            # have created. Found the hard way while sabotage-testing this.
            created_orgs.append(response.json()["org_id"])
        assert response.status_code == 422, f"{label!r} was accepted"


async def test_the_purge_covers_every_org_scoped_table(org_scoped_tables) -> None:
    """Tripwire for the day the schema grows.

    Migration 0004 names the child tables it clears rather than discovering
    them, which buys a purge a reviewer can audit in one read at the cost of a
    list that can fall behind. This is what makes falling behind loud: add a
    table with an `org_id` column and this fails immediately, naming it, before
    anyone can ship a purge that silently retains it.
    """
    assert set(org_scoped_tables) == set(CHILD_COUNT_SQL), (
        "the org-scoped tables in the schema no longer match the ones the purge tests "
        "cover; extend migration 0004's statement and CHILD_COUNT_SQL together: "
        f"{sorted(set(org_scoped_tables) ^ set(CHILD_COUNT_SQL))}"
    )


async def test_purge_removes_expired_orgs_only(worker_role, created_orgs: list[str]) -> None:
    """Drain first, then measure.

    The development database outlives any single run, so an unconditional
    `== 1` would silently count other runs' leftovers. Purging once up front
    makes the second call's count mean exactly "the organisation this test
    created", and the identity assertions below carry the rest of the weight.
    """
    await _run_purge()

    expired = await _seed_org(created_orgs, expires_at=datetime.now(UTC) - timedelta(hours=1))
    live = await _seed_org(created_orgs, expires_at=datetime.now(UTC) + DEMO_ORG_TTL)

    removed = await _run_purge()
    assert removed == 1, f"expected exactly the one expired organisation, purged {removed}"

    async with tenant_session(expired) as session:
        assert (
            await session.scalar(
                text("SELECT count(*) FROM organizations WHERE id = :org"), {"org": expired}
            )
            == 0
        ), "the expired organisation survived the purge"
        for table, sql in CHILD_COUNT_SQL.items():
            assert await session.scalar(text(sql), {"org": expired}) == 0, (
                f"{table} still holds rows belonging to a purged organisation — this schema "
                "has no foreign keys, so deleting the organisation row cascades to nothing"
            )

    async with tenant_session(live) as session:
        assert (
            await session.scalar(
                text("SELECT count(*) FROM organizations WHERE id = :org"), {"org": live}
            )
            == 1
        ), "the purge removed an organisation that had not expired"
        for table, sql in CHILD_COUNT_SQL.items():
            assert await session.scalar(text(sql), {"org": live}) == 1, (
                f"the purge removed {table} rows belonging to a live organisation"
            )


async def test_the_purge_never_touches_rows_it_did_not_just_destroy(
    worker_role, created_orgs: list[str]
) -> None:
    """The sweep is keyed on the organisations this run destroyed, and must stay so.

    An earlier version deleted every child row whose org_id had no
    `organizations` row, justified as "safe by construction: a child is only ever
    created after its organisation". This schema has no foreign keys at all, so
    nothing enforces that ordering — a transaction that inserts an organisation
    while another session commits a child row for it, and only then commits,
    loses that child to an unfiltered cross-tenant DELETE. A live tenant's data,
    destroyed on a convention.

    A row with no organisation is therefore left strictly alone. This is the
    inverse of what this test used to assert, and deliberately so: the purge's
    reach is now bounded by identity rather than by an ordering claim, and this
    is what fails if anyone reinstates the anti-join.
    """
    await _run_purge()

    stranger = str(uuid.uuid4())
    conversation_id = str(uuid.uuid4())
    async with tenant_session(stranger) as session:
        await session.execute(
            text("INSERT INTO conversations (id, org_id, title) VALUES (:i, :o, 'no org yet')"),
            {"i": conversation_id, "o": stranger},
        )
    created_orgs.append(stranger)

    expired = await _seed_org(created_orgs, expires_at=datetime.now(UTC) - timedelta(hours=1))
    removed = await _run_purge()
    assert removed == 1, f"expected exactly the one expired organisation, purged {removed}"

    async with tenant_session(stranger) as session:
        survived = await session.scalar(
            text("SELECT count(*) FROM conversations WHERE org_id = :org"), {"org": stranger}
        )
    assert survived == 1, (
        "the purge deleted a row belonging to an organisation it never destroyed — that is "
        "the blind anti-join, and it takes live tenants' data with it"
    )
    async with tenant_session(expired) as session:
        assert (
            await session.scalar(
                text("SELECT count(*) FROM conversations WHERE org_id = :org"), {"org": expired}
            )
            == 0
        )

async def test_a_row_committed_while_the_purge_waits_is_still_collected(
    worker_role, created_orgs: list[str]
) -> None:
    """The race the two-statement shape exists to close.

    Statement 1 blocks on the expiring organisation's row lock. A tenant that
    commits a conversation while it waits is invisible to that statement's
    snapshot. If the child deletes rode along in the same statement, the
    organisation would go and the row would stay — unreachable forever, since RLS
    keys on an org id nobody holds and later runs only scan `organizations`.
    Statement 2 runs on a fresh snapshot, which is the whole point of splitting.

    Deliberately not routed through the Celery task: this is about the SQL's
    behaviour under concurrency, and holding a transaction open across the task's
    own event loop would test the harness instead.

    The race is *observed*, not slept through — see
    `_wait_until_the_purge_blocks_on_a_lock`.
    """
    await _run_purge()
    org = await _seed_org(created_orgs, expires_at=datetime.now(UTC) - timedelta(hours=1))

    from app.db.engine import engine
    from app.db.session import session_factory

    # A third connection, because the worker_role fixture's engine allows exactly
    # one and the holder below is sitting on it.
    observer = _worker_engine()
    holder = session_factory(info={"org_id": org, "tenant_scoped": True})
    purge: asyncio.Task[int] | None = None
    try:
        async with holder.begin():
            # The holder is not expected to wait for anything, so a wait here is
            # a leftover lock from an earlier run rather than this test's race.
            # Bounding it turns that into a failure instead of a hang.
            await holder.execute(text("SET LOCAL lock_timeout = '60s'"))
            await holder.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"), {"org": org}
            )
            # Take the row lock the purge will block on.
            await holder.execute(
                text("SELECT id FROM organizations WHERE id = :org FOR UPDATE"), {"org": org}
            )

            purge = asyncio.create_task(asyncio.to_thread(_purge_in_a_new_loop))
            await _wait_until_the_purge_blocks_on_a_lock(observer)

            await holder.execute(
                text(
                    "INSERT INTO conversations (id, org_id, title) "
                    "VALUES (:i, :o, 'committed mid-purge')"
                ),
                {"i": str(uuid.uuid4()), "o": org},
            )
        # holder commits here; the purge unblocks
        removed = await purge
    finally:
        # However this exits, the holder's transaction is over by now and the
        # purge is unblocked. Drain it rather than leave a thread writing into a
        # database the next test is about to measure.
        if purge is not None and not purge.done():
            with contextlib.suppress(Exception):
                await purge
        await holder.close()
        await observer.dispose()
        await engine.dispose()

    assert removed == 1, f"the expired organisation was not purged (got {removed})"
    async with tenant_session(org) as session:
        left = await session.scalar(
            text("SELECT count(*) FROM conversations WHERE org_id = :org"), {"org": org}
        )
    assert left == 0, (
        "a row committed while the purge was blocked outlived its organisation; nothing in "
        "the system can reach it again"
    )

async def test_an_expired_organisation_stays_invisible_to_other_tenants(
    created_orgs: list[str],
) -> None:
    """Expiry is a reason to delete a tenant, never a reason to publish it.

    Both routes an expired organisation can leak through are checked, because
    they fail independently. The *policy* route is another tenant's session: a
    second, permissive SELECT policy guarded by `expires_at < now()` is the
    obvious way to give a purge cross-organisation reach, and permissive
    policies OR together, so it would hand every tenant read access to every
    expired tenant's label. The *unscoped* route is a session with no tenant
    context at all, which is what a helper function or a system-role grant
    opens — and it is invisible from inside a tenant session, so checking only
    the first would leave this docstring claiming more than the test knows.
    """
    expired = await _seed_org(created_orgs, expires_at=datetime.now(UTC) - timedelta(hours=1))
    other = await _seed_org(created_orgs, expires_at=datetime.now(UTC) + DEMO_ORG_TTL)

    async with tenant_session(other) as session:
        visible = {
            str(i) for i in (await session.execute(text("SELECT id FROM organizations"))).scalars()
        }
        assert other in visible, "a tenant must still see itself"
        assert expired not in visible, "an expired organisation leaked to an unrelated tenant"
        assert (
            await session.scalar(
                text("SELECT name FROM organizations WHERE id = :org"), {"org": expired}
            )
            is None
        )
        result = await session.execute(
            text("DELETE FROM organizations WHERE id = :org"), {"org": expired}
        )
        assert result.rowcount == 0, "a tenant deleted an expired organisation that is not its own"

    async with system_session() as session:
        count = await session.scalar(text("SELECT count(*) FROM organizations"))
    assert count == 0, "an unscoped session can enumerate organisations"


async def test_an_unscoped_application_session_can_see_no_organisation(
    created_orgs: list[str],
) -> None:
    """The property. Everything else in this file exists to keep it true.

    An organisation id is the RLS credential, not an identifier. Every policy in
    migrations 0002 and 0003 keys off `current_setting('app.current_org_id')`,
    so anything that tells an application role which ids exist lets that role
    set the GUC to one and then read and write that tenant's organizations,
    conversations, messages and document_chunks — the whole isolation claim
    defeated by a lookup. With no such route, an id has to be guessed, and it is
    a v4 UUID.

    An organisation is seeded first on purpose. `count(*) == 0` against an empty
    table is true for the wrong reason, and this test would then pass in a
    database where the property had been broken.
    """
    seeded = await _seed_org(created_orgs, expires_at=datetime.now(UTC) + DEMO_ORG_TTL)

    async with tenant_session(seeded) as session:
        assert await session.scalar(text("SELECT count(*) FROM organizations")) == 1, (
            "the seed did not land, so the unscoped count below would prove nothing"
        )

    async with system_session() as session:
        connected_as = await session.scalar(text("SELECT current_user"))
        assert connected_as == "lexloom_app", (
            f"this test only means something as an unprivileged role; it ran as {connected_as!r}"
        )
        visible = await session.scalar(text("SELECT count(*) FROM organizations"))
    assert visible == 0, (
        "a session with no tenant context can enumerate organisations, which hands it the "
        "RLS credential for every tenant in the database"
    )


async def test_the_database_holds_no_relation_outside_the_inventory() -> None:
    """Every user-created relation, in every schema, named in advance.

    This replaced a guard that asked which relations an application role could
    SELECT from. That question was answered wrongly twice — `GRANT SELECT (id)`
    on one column makes `has_table_privilege(…, 'SELECT')` false while the id is
    perfectly readable, and the `information_schema` carve-out was justified by
    checking that the application roles cannot CREATE there, when the objects it
    existed to catch are created by the owner, who can. Both bypasses are
    reachability questions. This assertion asks none.

    What it catches, in one comparison and with no privilege semantics: a view
    over `organizations`, whether or not it carries `security_invoker`, whether
    or not it is granted to anyone, in `public` or in `information_schema` or in
    a schema created for the purpose; a materialised view holding a snapshot of
    the ids; a foreign table; a table nobody policied. Anything a migration adds
    turns this red and names it, which is the moment to decide whether it needs
    RLS — and the moment to extend the inventory deliberately rather than to
    discover a year later that it was never covered.
    """
    async with system_session() as session:
        rows = (await session.execute(text(USER_CREATED_RELATIONS_SQL))).all()
    found = {(row.schema_name, row.relation_name, row.relation_kind) for row in rows}
    assert found == EXPECTED_RELATIONS, (
        "the set of user-created relations is not the one migrations 0001-0004 create. "
        "Unexpected (present but not in the inventory): "
        f"{sorted(found - EXPECTED_RELATIONS)}. Missing (in the inventory but not present): "
        f"{sorted(EXPECTED_RELATIONS - found)}"
    )


async def test_the_database_holds_no_function_outside_the_inventory() -> None:
    """Every user-created function, in every schema, named in advance.

    This replaced a guard that asked which SECURITY DEFINER functions an
    application role could EXECUTE. `pg_has_role(…, 'MEMBER')` can be true while
    both USAGE and SET are false, so a MEMBER-only grant walked past it; and
    Postgres checks EXECUTE on a trigger function at `CREATE TRIGGER` time
    rather than at fire time, so a SECURITY DEFINER trigger function with no
    EXECUTE grant *at all* ran on an ordinary INSERT while the guard read its
    ACL as empty and stayed green. Both are reachability questions. This
    assertion asks none: the function is either in the catalog or it is not.

    The extension list is asserted first because it is what bounds the one
    exclusion this inventory makes — see USER_CREATED_FUNCTIONS_SQL.
    """
    async with system_session() as session:
        extensions = set(
            (await session.execute(text("SELECT extname FROM pg_catalog.pg_extension"))).scalars()
        )
        rows = (await session.execute(text(USER_CREATED_FUNCTIONS_SQL))).all()

    assert extensions == EXPECTED_EXTENSIONS, (
        "the set of installed extensions changed, so the extension exclusion in the function "
        "inventory no longer covers what it was written for: "
        f"{sorted(extensions ^ EXPECTED_EXTENSIONS)}"
    )

    found = {
        (row.schema_name, row.function_name, row.arguments, row.kind, row.security_definer)
        for row in rows
    }
    assert found == EXPECTED_FUNCTIONS, (
        "the set of user-created functions is not the one migration 0004 creates. "
        "Unexpected (present but not in the inventory): "
        f"{sorted(found - EXPECTED_FUNCTIONS)}. Missing (in the inventory but not present): "
        f"{sorted(EXPECTED_FUNCTIONS - found)}"
    )


async def test_the_purge_helper_is_pinned_by_value_and_not_granted_to_public() -> None:
    """The purge's whole security rests on two function settings, so pin both.

    `search_path` is asserted by value, not by presence. `SET search_path =
    public, pg_catalog` is a pin, and it is the dangerous one: pg_catalog listed
    last loses its implicit-first placement, so `now()` in the body resolves
    through `public` and a hostile `public.now()` returning 'infinity' makes
    every live tenant look expired. The body schema-qualifies its calls so this
    is a second line rather than the only one, but a reordered pin must still
    fail here.

    `row_security = off` is what stops the purge going silently dead on the
    deployment the FORCE comments in 0002/0003 promise — an owner that is not a
    superuser, where RLS applies to the owner and no policy grants it anything.
    """
    async with system_session() as session:
        row = (await session.execute(text(PURGE_FUNCTION_SQL))).one()
    assert row.prosecdef, "purge_expired_orgs is not SECURITY DEFINER, so it cannot see"
    assert EXPECTED_PURGE_FUNCTION_SETTINGS.issubset(set(row.proconfig or ())), (
        f"purge_expired_orgs settings are {row.proconfig}, expected to include "
        f"{sorted(EXPECTED_PURGE_FUNCTION_SETTINGS)}"
    )
    assert not row.public_executes, "purge_expired_orgs is executable by PUBLIC"


async def test_the_purge_helper_is_not_executable_by_the_api_role() -> None:
    """Only the worker may destroy tenants.

    The suite connects as lexloom_app, so this runs on the default engine and
    the denial is the assertion. Widening the grant to make a purge test pass is
    the natural mistake; it hands anyone who reaches SQL through the API the
    ability to delete every expired tenant on demand, so it fails here loudly.
    """
    with pytest.raises(ProgrammingError, match="permission denied for function"):
        async with system_session() as session:
            await session.execute(text("SELECT public.purge_expired_orgs()"))

def _deadlock_error(sqlstate: str) -> DBAPIError:
    """A DBAPIError shaped the way SQLAlchemy actually delivers a Postgres error.

    Shaped from an observed one: a real deadlock through asyncpg arrives as a
    bare `DBAPIError` — not `OperationalError`, not a deadlock subclass — with
    the SQLSTATE only reachable on `.orig`.
    """

    class _Orig(Exception):
        def __init__(self) -> None:
            self.sqlstate = sqlstate

    return DBAPIError("SELECT public.purge_expired_orgs()", {}, _Orig())

def test_only_a_deadlock_counts_as_a_deadlock() -> None:
    """Retrying on `DBAPIError` itself would silently retry a permission denial
    or a syntax error three times before reporting it."""
    assert is_deadlock(_deadlock_error("40P01"))
    assert not is_deadlock(_deadlock_error("42501"))  # permission denied
    assert not is_deadlock(RuntimeError("something else entirely"))

async def test_the_purge_task_retries_a_deadlock_and_propagates_anything_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The purge is one statement, so losing it to a deadlock loses the whole run.

    Postgres picks a victim when it breaks a lock cycle, and the purge — which
    takes organisations before children, the opposite order from a handler
    updating a message and then its own organisation row — can be the one
    killed. Without a retry the next attempt is a whole beat interval away.

    `retry` is replaced rather than exercised. Celery's retry machinery is
    Celery's to get right; what belongs here is which of the two branches this
    task chooses, and `.apply()` cannot show that — it runs with
    `called_directly=True`, under which `retry` re-raises the original error and
    a retried deadlock becomes indistinguishable from an unretried one.
    """
    recorded: list[tuple[BaseException | None, int | None]] = []

    def _fake_retry(exc: BaseException | None = None, countdown: int | None = None, **_: object):
        recorded.append((exc, countdown))
        raise RuntimeError("retry() was called")

    monkeypatch.setattr(purge_expired_orgs, "retry", _fake_retry)

    def _always_raising(error: BaseException):
        async def _boom() -> int:
            raise error

        return _boom

    # `.apply()` without `.get()`: Celery makes the stored exception picklable
    # for the result backend, and DBAPIError is not — its __init__ takes three
    # positional arguments — so `.get()` re-raises it downgraded to a base class
    # and `pytest.raises(DBAPIError)` would never match. Which branch the task
    # took is the question anyway, and `recorded` answers it exactly.
    deadlock = _deadlock_error("40P01")
    monkeypatch.setattr("app.auth.demo.purge_expired_orgs", _always_raising(deadlock))
    await asyncio.to_thread(purge_expired_orgs.apply)
    assert recorded == [(deadlock, 5)], "the deadlock was not handed to retry with a countdown"

    denied = _deadlock_error("42501")
    monkeypatch.setattr("app.auth.demo.purge_expired_orgs", _always_raising(denied))
    result = await asyncio.to_thread(purge_expired_orgs.apply)
    assert len(recorded) == 1, "a non-deadlock failure was retried instead of raised"
    assert result.state == "FAILURE", "a non-deadlock failure did not surface as a failure"

def test_the_worker_dsn_is_derived_rather_than_string_replaced() -> None:
    """The worker DSN must follow a customised application password.

    A `DATABASE_URL.replace("lexloom_app:app", "lexloom_worker:worker")` matches
    only the default credentials. Set APP_DB_PASSWORD to anything and it returns
    the URL untouched, so every test that claims to run as the worker runs as
    lexloom_app instead — failing on the missing grant, which sends the reader
    to migration 0004 rather than to the one line that is actually wrong.
    """
    # Imported here rather than at module scope: `conftest` is not an installed
    # package, and hoisting it puts a local helper in the third-party import
    # block where the next reader will assume it comes from a dependency.
    from conftest import worker_dsn_from

    app_dsn = "postgresql+asyncpg://lexloom_app:s3cret@db.internal:6000/lexloom"
    worker_password = "w0rker"  # noqa: S105  (a literal in a pure-function test)
    derived = make_url(worker_dsn_from(app_dsn, worker_password))
    assert derived.username == "lexloom_worker"
    assert derived.password == worker_password
    assert (derived.host, derived.port, derived.database) == ("db.internal", 6000, "lexloom")

def test_the_demo_token_cannot_outlive_the_organisation_it_names() -> None:
    """The purge's residual window is closed by these two constants, nothing else.

    The sweep only reaches organisations the run itself destroyed, so a child row
    committed after it would never be collected. Nothing can produce one today:
    writing to a child table requires `app.current_org_id` to be that
    organisation, which requires a valid token for it, and a demo token expires
    long before the organisation does.

    That is an argument about two numbers, so this half of it is asserted as
    one: lengthen the token past the organisation and it fails. The *other* half
    — "and no endpoint hands out a fresh one" — is not a statement about
    constants and cannot be checked here; it is checked by
    `test_the_route_surface_is_the_one_the_residual_argument_was_written_for`.
    """
    assert timedelta(seconds=DEMO_TOKEN_TTL_SECONDS) < DEMO_ORG_TTL, (
        "a demo token can now outlive its organisation, so a token holder can write rows "
        "into an organisation the purge has already destroyed and nothing will collect them"
    )


def _iter_routes(routes: object) -> object:
    """Flatten FastAPI's route tree, and refuse to guess when it changes shape.

    `app.routes` is not flat: since FastAPI 0.13x an `include_router` call
    leaves a single `_IncludedRouter` node whose children hang off
    `original_router.routes`. A walker that silently skipped a node it did not
    recognise would under-report the surface, which for an inventory is the one
    failure mode worse than being wrong — so an unrecognised node raises.
    """
    for route in routes:
        children = getattr(route, "routes", None)
        if children is None:
            included = getattr(route, "original_router", None)
            children = None if included is None else included.routes
        if children is not None:
            yield from _iter_routes(children)
        elif hasattr(route, "path"):
            yield route
        else:
            raise AssertionError(
                f"{type(route).__name__} exposes neither a path nor child routes, so this "
                "inventory cannot see through it; the FastAPI route tree has changed shape"
            )


# Path and methods for every route the application serves, including the four
# FastAPI mounts by default. `HEAD` appears on those four because Starlette's
# plain `Route` adds it and FastAPI's `APIRoute` does not — recorded as observed
# rather than tidied, so the literal keeps matching what the framework does.
EXPECTED_ROUTES = {
    ("/openapi.json", frozenset({"GET", "HEAD"})),
    ("/docs", frozenset({"GET", "HEAD"})),
    ("/docs/oauth2-redirect", frozenset({"GET", "HEAD"})),
    ("/redoc", frozenset({"GET", "HEAD"})),
    ("/health/live", frozenset({"GET"})),
    ("/health/ready", frozenset({"GET"})),
    ("/.well-known/jwks.json", frozenset({"GET"})),
    ("/api/v1/auth/demo-login", frozenset({"POST"})),
}


def test_the_route_surface_is_the_one_the_residual_argument_was_written_for(app) -> None:
    """The purge's residual argument rests on a claim about *features*, so
    inventory the features.

    Migration 0004 and the test above say the argument fails "the moment a
    refresh endpoint or a longer token appears". The longer-token half is two
    constants and is asserted directly. The refresh-endpoint half was not
    asserted at all: adding a real `POST /api/v1/auth/refresh` that mints a
    fresh token for a caller-supplied org_id left the whole suite green, while
    making the residual window arbitrarily wide — a visitor could hold a live
    token for an organisation the purge had already destroyed and write child
    rows nothing would ever collect.

    A test cannot notice a feature unless it inventories features. So the route
    set is a literal, and any new endpoint turns this red. That is not an
    objection to the endpoint; it is the moment its author has to read migration
    0004's residual argument and decide whether it still holds. Extending this
    list is a one-line change once they have.
    """
    found = {(route.path, frozenset(route.methods or ())) for route in _iter_routes(app.routes)}
    assert found == EXPECTED_ROUTES, (
        "the application's route surface changed, which is what invalidates the purge's "
        "residual safety argument (migration 0004, and "
        "test_the_demo_token_cannot_outlive_the_organisation_it_names above). Added: "
        f"{sorted(found - EXPECTED_ROUTES)}. Removed: {sorted(EXPECTED_ROUTES - found)}"
    )
