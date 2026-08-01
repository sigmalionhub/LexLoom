import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.db.engine import OWNED_TABLES_SQL, UnsafeDatabaseRoleError, assert_unprivileged_role


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


async def test_owned_tables_query_separates_app_from_migrator() -> None:
    """The ownership branch only matters if this query tells the roles apart.

    lexloom_app never owns any table in public — it is granted privileges and
    creates nothing, per deploy/postgres/init/01-roles.sh and the GRANTs in the
    migrations; lexloom_migrator owns everything it created via Alembic,
    including organizations. Proving the query returns 0 for one and a
    non-zero count for the other is what the ownership check in
    assert_unprivileged_role actually depends on.
    """
    app_engine = create_async_engine(get_settings().database_url)
    async with app_engine.connect() as conn:
        app_owned = (await conn.execute(text(OWNED_TABLES_SQL))).scalar_one()
    await app_engine.dispose()
    assert app_owned == 0

    migrator_dsn = get_settings().database_url.replace(
        "lexloom_app:app", "lexloom_migrator:migrator"
    )
    migrator_engine = create_async_engine(migrator_dsn)
    async with migrator_engine.connect() as conn:
        migrator_owned = (await conn.execute(text(OWNED_TABLES_SQL))).scalar_one()
    await migrator_engine.dispose()
    assert migrator_owned > 0
