from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, async_sessionmaker, create_async_engine

from app.config import get_settings


class UnsafeDatabaseRoleError(RuntimeError):
    """The connected role can bypass row-level security."""


_settings = get_settings()

engine = create_async_engine(
    _settings.database_url,
    pool_size=10,
    max_overflow=10,
    pool_pre_ping=True,
)

session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def assert_unprivileged_role(conn: AsyncConnection) -> None:
    """Refuse to run as a role that ignores RLS.

    A superuser, a role with BYPASSRLS, or the table owner without FORCE
    ROW LEVEL SECURITY all read every tenant's rows. Failing at startup is
    the only way this is caught before it matters.
    """
    row = (
        await conn.execute(
            text(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
            )
        )
    ).one()
    if row.rolsuper or row.rolbypassrls:
        raise UnsafeDatabaseRoleError(
            f"Refusing to start: current_user has rolsuper={row.rolsuper}, "
            f"rolbypassrls={row.rolbypassrls}"
        )
