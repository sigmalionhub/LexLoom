import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.db.session import system_session, tenant_session

# How long a visitor's throwaway tenant keeps its data.
DEMO_ORG_TTL = timedelta(hours=24)

# How long the token minted alongside it stays valid. Deliberately shorter than
# the organisation: a stolen demo token stops working long before the data it
# reaches is collected, and the visitor can log in again without losing it.
# Both numbers live here so the endpoint that reports one and the purge that
# enforces the other cannot drift apart.
DEMO_TOKEN_TTL_SECONDS = 3600

# Every table in `public` carrying an `org_id` column belongs to some tenant.
# Discovered from the catalog rather than listed, because this schema has no
# foreign keys — deliberately, since FK referential-integrity checks bypass RLS
# — so nothing cascades and a table added tomorrow would otherwise keep a
# purged tenant's rows forever, which is precisely the promise the demo makes.
# This is the same discovery the isolation invariants test runs, so the two
# agree by construction rather than by review.
ORG_SCOPED_TABLES_SQL = """
SELECT DISTINCT c.oid::regclass::text AS relation
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
WHERE a.attname = 'org_id'
  AND a.attnum > 0
  AND NOT a.attisdropped
  AND c.relkind = 'r'
  AND c.relnamespace = 'public'::regnamespace
"""


@dataclass(frozen=True)
class DemoOrganisation:
    id: str
    external_id: str
    name: str


async def provision_demo_org(label: str) -> DemoOrganisation:
    """Create a throwaway organisation for a visitor who has no tenant yet.

    Unscoped on purpose: the caller is anonymous and has no organisation to
    scope to, which is exactly why migration 0003 leaves INSERT on
    `organizations` unconditional while every other command is self-only.

    A plain INSERT, with no ON CONFLICT clause. `external_id` is a fresh uuid4
    so a conflict cannot occur, and reaching for `ON CONFLICT` here would make
    things worse rather than safer: with an explicit conflict target Postgres
    runs the SELECT policy's qual as the conflict check, which under 0003's
    per-command policies never matches, so an unauthenticated caller gets a
    42501 unconditionally — with no conflict present. Without a target it does
    not error but silently no-ops against a colliding row in *any* tenant,
    which is an existence oracle for other organisations' external ids.
    """
    org_id = uuid.uuid4()
    external_id = f"demo_{org_id.hex}"
    async with system_session() as session:
        await session.execute(
            text(
                "INSERT INTO organizations (id, external_id, name, expires_at) "
                "VALUES (:i, :e, :n, :x)"
            ),
            {
                "i": str(org_id),
                "e": external_id,
                "n": label,
                "x": datetime.now(UTC) + DEMO_ORG_TTL,
            },
        )
    return DemoOrganisation(id=str(org_id), external_id=external_id, name=label)


async def purge_expired_orgs() -> int:
    """Delete every organisation past its TTL, with all of its data. Returns
    the number of organisations removed.

    Two things about this that are not obvious from the SQL:

    The expired ids come from `expired_org_ids()` (migration 0004) rather than
    from a SELECT, because an unscoped SELECT on `organizations` returns zero
    rows under 0003's policies — the naive purge is not slow or partial, it is
    a silent no-op that reports 0 and looks like "nothing to do".

    The deletes then run inside each expiring organisation's *own* tenant
    context. That is not a formality: it is the reason no new cross-tenant
    grant exists anywhere in this path. Each statement is permitted by the
    same policy that would permit the tenant itself, so a bug here can only
    ever damage the tenant that was already being destroyed.
    """
    async with system_session() as session:
        expired = [
            str(value)
            for value in (
                await session.execute(text("SELECT * FROM public.expired_org_ids()"))
            ).scalars()
        ]
        if not expired:
            return 0
        child_tables = (await session.execute(text(ORG_SCOPED_TABLES_SQL))).scalars().all()

    removed = 0
    for org_id in expired:
        async with tenant_session(org_id) as session:
            for table in child_tables:
                # Table names come from pg_catalog already quoted by
                # `regclass::text`, never from a caller. The org_id filter is
                # redundant under RLS and kept anyway: if the tenant context
                # ever failed to apply, an unfiltered DELETE would empty the
                # table for every tenant at once.
                await session.execute(
                    text(f"DELETE FROM {table} WHERE org_id = :org"),  # noqa: S608
                    {"org": org_id},
                )
            result = await session.execute(
                text("DELETE FROM organizations WHERE id = :org"), {"org": org_id}
            )
            removed += result.rowcount or 0
    return removed
