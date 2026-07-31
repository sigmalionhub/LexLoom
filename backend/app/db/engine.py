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

# Every role the connected role is a direct member of, read straight out of
# `pg_auth_members` rather than through a privilege function.
#
# `pg_has_role` and `has_table_privilege` answer "can this role do X *right
# now*", and that is the wrong question here: `GRANT lexloom_migrator TO
# lexloom_app WITH INHERIT FALSE, SET TRUE` leaves every privilege check
# reporting exactly what it reported before the grant, and one `SET ROLE
# lexloom_migrator` then reads every row in the database. The membership row
# itself is what changed, so the membership row is what is checked.
#
# Direct membership is enough: a chain reaching the owner has to start with one
# grant naming this role as its member, and that grant appears here.
MEMBERSHIPS_SQL = """
SELECT g.rolname
FROM pg_catalog.pg_auth_members m
JOIN pg_catalog.pg_roles g ON g.oid = m.roleid
JOIN pg_catalog.pg_roles r ON r.oid = m.member
WHERE r.rolname = current_user
ORDER BY g.rolname
"""


async def assert_unprivileged_role(conn: AsyncConnection) -> None:
    """Refuse to run as a role that can ignore row-level security.

    There are four distinct bypasses, and from the application's side all four
    look identical — queries quietly return rows they should not:

    * a superuser ignores RLS entirely;
    * a role with BYPASSRLS does too;
    * the *owner* of a table bypasses that table's policies regardless of
      either flag, unless the table is marked FORCE ROW LEVEL SECURITY;
    * a role that is a *member* of any of the above can become one with
      `SET ROLE`, and until it does, every privilege check in the database
      reports it as unprivileged.

    Checking only the first two would leave the third open — and the third is
    the one a well-meaning deployment actually produces, by reusing the
    migration role for the application. The fourth is the same mistake made
    politely: `GRANT lexloom_migrator TO lexloom_app WITH INHERIT FALSE, SET
    TRUE` is the idiomatic shape for "a privileged role you step into
    deliberately", and it boots clean past the first three. Failing at startup
    is the only point where any of this is cheap to catch.

    Membership is rejected outright rather than followed to see where it leads.
    Deciding whether a particular grant is harmless means re-deriving the
    reachability question this repository has already got wrong five times; an
    application role in this design needs no membership in anything, so the
    safe set is empty and the check is an equality rather than a search.

    Not checked here, deliberately: REPLICATION, which reads the WAL and so
    sees every row of every tenant — but only over a replication connection,
    which this process never opens, so it is not a bypass *for this process*.
    It is pinned instead by the catalog inventory in
    `tests/test_demo_orgs.py`, which is about the deployment rather than about
    this connection.
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

    memberships = (await conn.execute(text(MEMBERSHIPS_SQL))).scalars().all()
    if memberships:
        raise UnsafeDatabaseRoleError(
            f"Refusing to start: current_user is a member of {', '.join(memberships)}. "
            "A member can SET ROLE to any of those and act with their privileges, while "
            "every privilege check on current_user itself keeps reporting it as "
            "unprivileged. Revoke the membership."
        )
