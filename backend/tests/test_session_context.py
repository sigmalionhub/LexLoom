import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db.engine import session_factory
from app.db.session import MissingTenantContextError, tenant_session

ORG_A = "11111111-1111-1111-1111-111111111111"


async def test_guc_is_set_inside_the_transaction() -> None:
    async with tenant_session(ORG_A) as session:
        current = await session.scalar(text("SELECT current_setting('app.current_org_id', true)"))
        assert current == ORG_A


async def test_guc_does_not_leak_to_the_next_session() -> None:
    """A session-scoped SET would survive the connection returning to the pool
    and the next request for a different tenant would inherit it.

    Run against a one-connection pool so the second session is *forced* onto the
    same physical connection. With the default pool this test proves nothing:
    it would pass whenever the pool happens to hand out a different connection,
    which is exactly the case where a leak could not be observed anyway.
    """
    single = create_async_engine(get_settings().database_url, pool_size=1, max_overflow=0)
    factory = async_sessionmaker(single, expire_on_commit=False)
    try:
        first = factory(info={"org_id": ORG_A, "tenant_scoped": True})
        async with first.begin():
            await first.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"), {"org": ORG_A}
            )
        await first.close()

        second = factory(info={"tenant_scoped": False})
        async with second.begin():
            current = await second.scalar(
                text("SELECT current_setting('app.current_org_id', true)")
            )
        await second.close()
        assert current in (None, ""), "the setting outlived its transaction"
    finally:
        await single.dispose()


async def test_opening_a_tenant_session_without_an_org_is_refused() -> None:
    with pytest.raises(MissingTenantContextError):
        async with tenant_session(""):
            pass


async def test_the_structural_guard_raises_when_a_scoped_session_has_no_org() -> None:
    """Exercise the after_begin listener itself, not tenant_session's early check.

    `tenant_session("")` rejects on its own `if not org_id` before a session is
    ever created, so it never reaches the listener. Constructing the factory
    directly is the only way to prove the guard — the one thing the row-level
    security policies trust — actually fires.
    """
    session = session_factory(info={"tenant_scoped": True})
    with pytest.raises(MissingTenantContextError, match="without an organisation"):
        async with session.begin():
            await session.execute(text("SELECT 1"))
    await session.close()
