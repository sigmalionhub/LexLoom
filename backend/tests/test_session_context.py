import pytest
from sqlalchemy import text

from app.db.session import MissingTenantContextError, system_session, tenant_session

ORG_A = "11111111-1111-1111-1111-111111111111"


async def test_guc_is_set_inside_the_transaction() -> None:
    async with tenant_session(ORG_A) as session:
        current = await session.scalar(text("SELECT current_setting('app.current_org_id', true)"))
        assert current == ORG_A


async def test_guc_does_not_leak_to_the_next_session() -> None:
    """A session-scoped SET would survive the connection returning to the pool
    and the next request for a different tenant would inherit it."""
    async with tenant_session(ORG_A):
        pass
    async with system_session() as session:
        current = await session.scalar(text("SELECT current_setting('app.current_org_id', true)"))
        assert current in (None, "")


async def test_opening_a_tenant_session_without_an_org_is_refused() -> None:
    with pytest.raises(MissingTenantContextError):
        async with tenant_session(""):
            pass
