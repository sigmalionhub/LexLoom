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


OWNED_TABLES_SQL = """
SELECT count(*) FROM pg_class c
JOIN pg_roles r ON c.relowner = r.oid
WHERE r.rolname = current_user
  AND c.relkind = 'r'
  AND c.relnamespace = 'public'::regnamespace
"""


async def assert_unprivileged_role(conn: AsyncConnection) -> None:
    """Refuse to run as a role that can ignore row-level security.

    There are three distinct bypasses, and from the application's side all
    three look identical — queries quietly return rows they should not:

    * a superuser ignores RLS entirely;
    * a role with BYPASSRLS does too;
    * the *owner* of a table bypasses that table's policies regardless of
      either flag, unless the table is marked FORCE ROW LEVEL SECURITY.

    Checking only the first two would leave the third open — and the third is
    the one a well-meaning deployment actually produces, by reusing the
    migration role for the application. Failing at startup is the only point
    where this is cheap to catch.
    """
    row = (
        await conn.execute(
            text(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
            )
        )
    ).one_or_none()
    if row is None:
        raise UnsafeDatabaseRoleError(
            "Refusing to start: current_user has no row in pg_roles"
        )
    if row.rolsuper or row.rolbypassrls:
        raise UnsafeDatabaseRoleError(
            f"Refusing to start: current_user has rolsuper={row.rolsuper}, "
            f"rolbypassrls={row.rolbypassrls}"
        )

    owned = (await conn.execute(text(OWNED_TABLES_SQL))).scalar_one()
    if owned:
        raise UnsafeDatabaseRoleError(
            f"Refusing to start: current_user owns {owned} table(s) in schema public. "
            "A table owner bypasses that table's RLS policies."
        )
