import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from httpx import AsyncClient
from sqlalchemy import text

from app.auth.demo import DEMO_ORG_TTL
from app.db.session import system_session, tenant_session
from app.worker.tasks import purge_expired_orgs

CHILD_COUNT_SQL = {
    "conversations": "SELECT count(*) FROM conversations WHERE org_id = :org",
    "messages": "SELECT count(*) FROM messages WHERE org_id = :org",
    "document_chunks": "SELECT count(*) FROM document_chunks WHERE org_id = :org",
}


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
    client: AsyncClient, created_orgs: list[str]
) -> None:
    """`expires_in` is a promise about the token, so derive it from the token.

    A literal in the response body agrees with the signed `exp` only by
    coincidence, and stops agreeing the moment either side is tuned.
    """
    body = (await client.post("/api/v1/auth/demo-login", json={"label": "Initech"})).json()
    created_orgs.append(body["org_id"])
    claims = jwt.decode(
        body["access_token"], options={"verify_signature": False}, audience="lexloom-api"
    )
    assert body["expires_in"] == claims["exp"] - claims["iat"]
    assert claims["org_id"] == body["org_id"]


async def test_purge_removes_expired_orgs_only(created_orgs: list[str]) -> None:
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

    The obvious way to give the purge reach across organisations is a second,
    permissive SELECT policy on organizations guarded by `expires_at < now()`.
    Permissive policies OR together, so that would hand every tenant read
    access to every expired tenant's label — a standing cross-organisation leak
    in the one table that names the tenants, bought to solve a problem that
    only the purge has. This test is what makes that trade visible.
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


async def test_the_expired_org_lookup_is_pinned_and_not_granted_to_public() -> None:
    """The purge's only cross-organisation capability, held to its bounds.

    `expired_org_ids()` runs as its owner, and the owner in this stack is the
    bootstrap superuser — so an unpinned search_path would let anyone able to
    create an object in a schema ahead of pg_catalog decide what `now()` means
    inside a superuser context. Neither application role can create in `public`
    today, which is a property of the current grants rather than of this
    function; the pin is what makes the function safe on its own terms.
    """
    async with system_session() as session:
        row = (
            await session.execute(
                text(
                    "SELECT prosecdef, proconfig, "
                    "       has_function_privilege('public', oid, 'EXECUTE') AS public_executes "
                    "FROM pg_proc WHERE oid = 'public.expired_org_ids()'::regprocedure"
                )
            )
        ).one()
    assert row.prosecdef, "expired_org_ids is not SECURITY DEFINER, so the purge cannot read"
    assert row.proconfig is not None and any(
        setting.startswith("search_path=") for setting in row.proconfig
    ), "expired_org_ids has no pinned search_path"
    assert not row.public_executes, "expired_org_ids is executable by PUBLIC"
