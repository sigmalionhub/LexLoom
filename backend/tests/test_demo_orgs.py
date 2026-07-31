import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from app.auth.demo import DEMO_ORG_TTL
from app.db.session import system_session, tenant_session
from app.worker.tasks import purge_expired_orgs

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

# Catalog-driven: no SECURITY DEFINER function in `public` may expose a uuid,
# by return type or by OUT parameter. Written against the whole schema rather
# than one function name, because the mistake this guards is a shape, not a
# spelling.
SECURITY_DEFINER_EXPOSURE_SQL = """
SELECT p.proname,
       pg_catalog.format_type(p.prorettype, NULL) AS return_type,
       coalesce(
         (SELECT string_agg(pg_catalog.format_type(t, NULL), ', ')
            FROM unnest(coalesce(p.proallargtypes, ARRAY[]::oid[])) AS t),
         ''
       ) AS declared_types
FROM pg_proc p
WHERE p.pronamespace = 'public'::regnamespace
  AND p.prosecdef
"""

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


async def test_demo_login_rejects_a_label_that_is_only_whitespace(
    client: AsyncClient, created_orgs: list[str]
) -> None:
    """An organisation named "" is indistinguishable in the UI from one whose
    name failed to load, and the row is unbounded-creation fuel either way."""
    for label in ("", "   "):
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


async def test_no_security_definer_function_hands_out_an_organisation_id() -> None:
    """An organisation id is the RLS credential, not an identifier.

    Every policy in migrations 0002 and 0003 keys off
    `current_setting('app.current_org_id')`. Anything that tells an application
    role which organisation ids exist lets that role set the GUC to one and then
    read and write that tenant's organizations, conversations, messages and
    document_chunks — the whole isolation claim defeated by a lookup. Without
    such a helper there is no enumeration path at all: an unscoped
    `SELECT count(*) FROM organizations` returns 0 and RLS suppresses the
    statistics views, so a v4 UUID has to be guessed.

    SECURITY DEFINER functions are where that leak can appear without touching a
    single policy, which is why this asks the catalog about the shape rather
    than about one function's name.
    """
    async with system_session() as session:
        rows = (await session.execute(text(SECURITY_DEFINER_EXPOSURE_SQL))).all()
    assert rows, "no SECURITY DEFINER functions found — the discovery query itself is broken"
    for name, return_type, declared_types in rows:
        assert "uuid" not in f"{return_type} {declared_types}", (
            f"SECURITY DEFINER function {name} exposes a uuid ({return_type}; "
            f"{declared_types}) — an organisation id is a credential, return a count"
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
