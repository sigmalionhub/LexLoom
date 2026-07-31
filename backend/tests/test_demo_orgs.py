import asyncio
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

# Every role an application role can *act as*, not merely the login role itself.
# `has_function_privilege('lexloom_app', …)` follows inherited membership only,
# and `GRANT r TO lexloom_app WITH INHERIT FALSE, SET TRUE` — the normal shape
# for a privileged role you step into deliberately — is invisible to it: the
# check says false, and `SET ROLE r` then does whatever r can do. `pg_has_role`
# with USAGE *or* SET covers both routes.
REACHABLE_ROLES_CTE = """
WITH login_role AS (
    SELECT unnest(ARRAY['lexloom_app', 'lexloom_worker']) AS name
),
reachable AS (
    SELECT l.name, r.oid AS role_oid
    FROM login_role l
    JOIN pg_roles r
      ON pg_catalog.pg_has_role(l.name, r.oid, 'USAGE')
      OR pg_catalog.pg_has_role(l.name, r.oid, 'SET')
)
"""

def _with_reachable_roles(query: str) -> str:
    """Both allowlists share one reachability rule, so they cannot drift apart."""
    return REACHABLE_ROLES_CTE + query


# Every SECURITY DEFINER function in the database, in every schema, with the
# effective reach of both application roles. `has_function_privilege` rather than
# parsing `proacl`: the question is what a role can actually do, which is not the
# same as what the ACL string literally says once PUBLIC grants and ownership are
# folded in.
SECURITY_DEFINER_REACH_SQL = _with_reachable_roles("""
SELECT n.nspname AS schema_name,
       p.proname AS function_name,
       pg_catalog.pg_get_function_identity_arguments(p.oid) AS arguments,
       pg_catalog.format_type(p.prorettype, NULL) AS return_type,
       p.proretset AS returns_set,
       EXISTS (SELECT 1 FROM reachable x
                WHERE x.name = 'lexloom_app'
                  AND pg_catalog.has_function_privilege(x.role_oid, p.oid, 'EXECUTE')
              ) AS app_reaches,
       EXISTS (SELECT 1 FROM reachable x
                WHERE x.name = 'lexloom_worker'
                  AND pg_catalog.has_function_privilege(x.role_oid, p.oid, 'EXECUTE')
              ) AS worker_reaches
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE p.prosecdef
""")

# The allowlist: (schema, name, identity arguments, return type, returns a set).
# Argument-less and returning one integer are part of the vetted shape, not
# incidental — a variant of this function taking an argument could be steered,
# and one returning rows could be made to return ids.
VETTED_SECURITY_DEFINER_FOR_WORKER = {("public", "purge_expired_orgs", "", "integer", False)}

# The same allowlist shape for relations. `prosecdef` says nothing about views,
# and a view owned by the table owner evaluates RLS *as the owner* unless it
# carries `security_invoker` — so a view over `organizations` is the C1
# escalation on a route the function guard cannot see.
READABLE_RELATIONS_SQL = _with_reachable_roles("""
SELECT n.nspname AS schema_name,
       c.relname AS relation_name,
       c.relkind::pg_catalog.text AS relation_kind,
       coalesce(
         (SELECT opt FROM unnest(c.reloptions) AS opt WHERE opt LIKE 'security_invoker=%'),
         ''
       ) AS security_invoker,
       EXISTS (SELECT 1 FROM reachable x
                WHERE x.name = 'lexloom_app'
                  AND pg_catalog.has_table_privilege(x.role_oid, c.oid, 'SELECT')
              ) AS app_reaches,
       EXISTS (SELECT 1 FROM reachable x
                WHERE x.name = 'lexloom_worker'
                  AND pg_catalog.has_table_privilege(x.role_oid, c.oid, 'SELECT')
              ) AS worker_reaches
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
""")

VETTED_READABLE_RELATIONS = {
    ("public", "organizations", "r"),
    ("public", "conversations", "r"),
    ("public", "messages", "r"),
    ("public", "document_chunks", "r"),
}

SECURITY_INVOKER_ON = frozenset(
    {"security_invoker=true", "security_invoker=on", "security_invoker=1", "security_invoker=yes"}
)

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


def _purge_in_a_new_loop() -> int:
    """Call the SQL function on its own connection, from a thread with no loop.

    Used only by the concurrency test, which has to hold one transaction open on
    the test's loop while another blocks inside the database. A separate engine
    keeps the two off the same pooled connection, which would otherwise
    deadlock the test rather than the purge.
    """
    import asyncio as _asyncio

    from conftest import WORKER_DATABASE_URL
    from sqlalchemy.ext.asyncio import create_async_engine

    async def _run() -> int:
        engine = create_async_engine(WORKER_DATABASE_URL, pool_size=1, max_overflow=0)
        try:
            async with engine.begin() as conn:
                return await conn.scalar(text("SELECT public.purge_expired_orgs()"))
        finally:
            await engine.dispose()

    return _asyncio.run(_run())

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

    Four input classes, each of which defeated an earlier version of the check:
    plain whitespace; U+200B ZERO WIDTH SPACE, which is not `str.isspace()` and
    so survives `strip_whitespace`; the Hangul and Braille fillers, which are
    ordinary `Lo`/`So` characters that happen to draw nothing and therefore
    cannot be excluded by any category rule; and bare combining marks, which have
    no base character to attach to. U+00A0 NO-BREAK SPACE is here for the
    opposite reason — Python strips it, Postgres would have stored it — so a
    future switch to a hand-rolled `.strip(" ")` fails loudly.

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
    """
    await _run_purge()
    org = await _seed_org(created_orgs, expires_at=datetime.now(UTC) - timedelta(hours=1))

    from app.db.engine import engine
    from app.db.session import session_factory

    holder = session_factory(info={"org_id": org, "tenant_scoped": True})
    try:
        async with holder.begin():
            await holder.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"), {"org": org}
            )
            # Take the row lock the purge will block on.
            await holder.execute(
                text("SELECT id FROM organizations WHERE id = :org FOR UPDATE"), {"org": org}
            )

            purge = asyncio.create_task(asyncio.to_thread(_purge_in_a_new_loop))
            await asyncio.sleep(0.4)  # long enough for statement 1 to be waiting

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
        await holder.close()
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


async def test_the_application_roles_can_reach_no_unvetted_security_definer_function() -> None:
    """An organisation id is the RLS credential, not an identifier.

    Every policy in migrations 0002 and 0003 keys off
    `current_setting('app.current_org_id')`. Anything that tells an application
    role which organisation ids exist lets that role set the GUC to one and then
    read and write that tenant's organizations, conversations, messages and
    document_chunks — the whole isolation claim defeated by a lookup. Without
    such a helper there is no enumeration path at all: an unscoped
    `SELECT count(*) FROM organizations` returns 0 and RLS suppresses the
    statistics views, so a v4 UUID has to be guessed.

    An earlier version of this test asked whether any definer function in
    `public` mentioned `uuid` in its signature. That guarded a type spelling,
    not a property: `RETURNS SETOF text` over `id::text` walks the same
    escalation past it, and so do `jsonb`, `json`, `varchar`, and anything at
    all in another schema. Enumerating the leaky shapes is a losing game.

    So the invariant is stated the other way round, as an allowlist over the
    whole catalog: an unprivileged role may execute no SECURITY DEFINER function
    at all, except the single vetted one, in the vetted shape. Then what the
    function returns stops mattering — an attacker's helper fails the assertion
    because it is executable, not because of how it spells its return type.

    "Executable" means by any role the login role can act as, inherited or
    stepped into with SET ROLE. `has_function_privilege('lexloom_app', …)` alone
    follows inheritance only, so `GRANT r TO lexloom_app WITH INHERIT FALSE, SET
    TRUE` reads as no access right up until the moment someone types SET ROLE.
    """
    async with system_session() as session:
        rows = (await session.execute(text(SECURITY_DEFINER_REACH_SQL))).all()
    assert rows, "no SECURITY DEFINER functions found — the discovery query itself is broken"

    reachable_by_api = [
        f"{row.schema_name}.{row.function_name}({row.arguments})"
        for row in rows
        if row.app_reaches
    ]
    assert not reachable_by_api, (
        "lexloom_app can execute SECURITY DEFINER functions, which run with the owner's "
        f"rights and therefore outside row-level security: {reachable_by_api}"
    )

    reachable_by_worker = {
        (row.schema_name, row.function_name, row.arguments, row.return_type, row.returns_set)
        for row in rows
        if row.worker_reaches
    }
    assert reachable_by_worker == VETTED_SECURITY_DEFINER_FOR_WORKER, (
        "the set of SECURITY DEFINER functions lexloom_worker can execute changed: "
        f"{sorted(reachable_by_worker ^ VETTED_SECURITY_DEFINER_FOR_WORKER)}"
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

async def test_the_application_roles_can_read_no_unvetted_relation() -> None:
    """`prosecdef` cannot see a view, and a view need not respect RLS.

    A view owned by the table owner evaluates its query with the *owner's*
    rights unless it is created `WITH (security_invoker = on)`. So a plain
    `CREATE VIEW org_directory AS SELECT id FROM organizations` hands every
    tenant every organisation id — the same escalation the SECURITY DEFINER
    allowlist exists to stop, arriving on a route that allowlist never looks at.
    Migration 0004's claim that there is no enumeration path is only true while
    no such relation exists, so state it as an invariant rather than an
    observation.

    Same shape as the function allowlist, and the same reachability rule: every
    relation either application role can SELECT from, in any schema, must be one
    of four tables. `pg_catalog` and `information_schema` are excluded because
    they are the platform's own catalogs, readable by every role in every
    PostgreSQL installation — and, checked here rather than assumed, neither
    application role can create anything in them.
    """
    async with system_session() as session:
        for role in ("lexloom_app", "lexloom_worker"):
            for schema in ("pg_catalog", "information_schema"):
                can_create = await session.scalar(
                    text("SELECT has_schema_privilege(:r, :s, 'CREATE')"),
                    {"r": role, "s": schema},
                )
                assert not can_create, (
                    f"{role} can create objects in {schema}, so excluding it from this "
                    "check is no longer safe"
                )
        rows = (await session.execute(text(READABLE_RELATIONS_SQL))).all()

    for name, expected in (("lexloom_app", "app_reaches"), ("lexloom_worker", "worker_reaches")):
        reachable = {
            (row.schema_name, row.relation_name, row.relation_kind)
            for row in rows
            if getattr(row, expected)
        }
        assert reachable == VETTED_READABLE_RELATIONS, (
            f"the set of relations {name} can read changed: "
            f"{sorted(reachable ^ VETTED_READABLE_RELATIONS)}"
        )

    # Belt and braces for the day a view is added deliberately and the allowlist
    # above is updated to match: it must still evaluate RLS as its caller.
    for row in rows:
        if row.relation_kind in ("v", "m") and (row.app_reaches or row.worker_reaches):
            assert row.security_invoker in SECURITY_INVOKER_ON, (
                f"{row.schema_name}.{row.relation_name} is readable by an application role "
                "but does not carry security_invoker, so it evaluates row-level security as "
                "its owner rather than as the caller"
            )

def test_the_demo_token_cannot_outlive_the_organisation_it_names() -> None:
    """The purge's residual window is closed by these two constants, nothing else.

    The sweep only reaches organisations the run itself destroyed, so a child row
    committed after it would never be collected. Nothing can produce one today:
    writing to a child table requires `app.current_org_id` to be that
    organisation, which requires a valid token for it, and a demo token expires
    long before the organisation does.

    That is an argument about two numbers, so it is asserted as one. Add a
    token-refresh endpoint, or lengthen the token, and this fails — which is the
    moment the purge would need a time-bounded sweep instead, and the moment
    `document_chunks` would need the `created_at` it does not have.
    """
    assert timedelta(seconds=DEMO_TOKEN_TTL_SECONDS) < DEMO_ORG_TTL, (
        "a demo token can now outlive its organisation, so a token holder can write rows "
        "into an organisation the purge has already destroyed and nothing will collect them"
    )
