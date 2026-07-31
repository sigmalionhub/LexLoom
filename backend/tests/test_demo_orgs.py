import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, ProgrammingError

from app.auth.demo import DEMO_ORG_TTL
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

# Every SECURITY DEFINER function in the database, in every schema, with the
# effective rights of both application roles. `has_function_privilege` rather
# than parsing `proacl`: the question is what a role can actually do, which is
# not the same as what the ACL string literally says once role membership,
# PUBLIC grants and ownership are folded in.
SECURITY_DEFINER_REACH_SQL = """
SELECT n.nspname AS schema_name,
       p.proname AS function_name,
       pg_catalog.pg_get_function_identity_arguments(p.oid) AS arguments,
       pg_catalog.format_type(p.prorettype, NULL) AS return_type,
       p.proretset AS returns_set,
       pg_catalog.has_function_privilege('lexloom_app', p.oid, 'EXECUTE') AS app_executes,
       pg_catalog.has_function_privilege('lexloom_worker', p.oid, 'EXECUTE') AS worker_executes
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE p.prosecdef
"""

# The allowlist: (schema, name, identity arguments, return type, returns a set).
# Argument-less and returning one integer are part of the vetted shape, not
# incidental — a variant of this function taking an argument could be steered,
# and one returning rows could be made to return ids.
VETTED_SECURITY_DEFINER_FOR_WORKER = {("public", "purge_expired_orgs", "", "integer", False)}

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

    U+200B ZERO WIDTH SPACE is in the list because it is not `str.isspace()`, so
    it survives `strip_whitespace` and produces exactly the same blank name from
    an input class a whitespace check never sees. U+00A0 NO-BREAK SPACE goes the
    other way — Python strips it, Postgres would have stored it — and is here so
    a future switch to a hand-rolled `.strip(" ")` fails loudly.
    """
    # Written as escapes on purpose: a literal zero-width space in source is
    # invisible to the next reader and survives a careless reformat unnoticed.
    for label in ("", "   ", "\u200b", "\u00a0", "\u200b\u00a0\u200b"):
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


async def test_the_purge_reclaims_rows_whose_organisation_is_already_gone(
    worker_role, created_orgs: list[str]
) -> None:
    """Rows outliving their organisation are unreachable, not merely untidy.

    The purge locks `organizations` rows, not their children. A tenant that
    commits a new conversation while the purge waits on that lock lands outside
    the purge's statement snapshot: the organisation goes, the row stays, and
    nothing can ever reach it again — every RLS policy on it keys on an org id
    nobody holds, and later runs only scan `organizations`. Silent permanent
    retention of a purged tenant's content is precisely what this feature exists
    to rule out.

    Reproduced deterministically rather than by racing: deleting only the
    organisation row leaves exactly the state that race produces.

    The count is asserted separately and must be **zero** — reclaiming stray
    rows may never be folded into "organisations destroyed", or a purge that
    tidied a thousand orphans and expired nothing would report a thousand dead
    tenants.
    """
    await _run_purge()

    org = await _seed_org(created_orgs, expires_at=None)
    async with tenant_session(org) as session:
        await session.execute(
            text("DELETE FROM organizations WHERE id = :org"), {"org": org}
        )
    async with tenant_session(org) as session:
        for table, sql in CHILD_COUNT_SQL.items():
            assert await session.scalar(text(sql), {"org": org}) == 1, (
                f"{table} row vanished before the purge ran — this test is not testing "
                "what it thinks it is"
            )

    removed = await _run_purge()
    assert removed == 0, (
        f"reclaimed orphan rows were counted as destroyed organisations (got {removed})"
    )

    async with tenant_session(org) as session:
        for table, sql in CHILD_COUNT_SQL.items():
            assert await session.scalar(text(sql), {"org": org}) == 0, (
                f"{table} still holds rows for an organisation that no longer exists; "
                "nothing else in the system can ever reach them"
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
    """
    async with system_session() as session:
        rows = (await session.execute(text(SECURITY_DEFINER_REACH_SQL))).all()
    assert rows, "no SECURITY DEFINER functions found — the discovery query itself is broken"

    reachable_by_api = [
        f"{row.schema_name}.{row.function_name}({row.arguments})"
        for row in rows
        if row.app_executes
    ]
    assert not reachable_by_api, (
        "lexloom_app can execute SECURITY DEFINER functions, which run with the owner's "
        f"rights and therefore outside row-level security: {reachable_by_api}"
    )

    reachable_by_worker = {
        (row.schema_name, row.function_name, row.arguments, row.return_type, row.returns_set)
        for row in rows
        if row.worker_executes
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
