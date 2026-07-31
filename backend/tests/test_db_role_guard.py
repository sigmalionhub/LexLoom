import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.db.engine import UnsafeDatabaseRoleError, assert_unprivileged_role


async def test_application_role_is_not_privileged() -> None:
    engine = create_async_engine(get_settings().database_url)
    async with engine.connect() as conn:
        await assert_unprivileged_role(conn)
    await engine.dispose()


async def test_guard_rejects_a_superuser_connection() -> None:
    dsn = get_settings().database_url.replace("lexloom_app:app", "lexloom_migrator:migrator")
    engine = create_async_engine(dsn)
    async with engine.connect() as conn:
        with pytest.raises(UnsafeDatabaseRoleError):
            await assert_unprivileged_role(conn)
    await engine.dispose()


async def test_organizations_table_exists() -> None:
    engine = create_async_engine(get_settings().database_url)
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT to_regclass('public.organizations')"))
        assert result.scalar() == "organizations"
    await engine.dispose()
