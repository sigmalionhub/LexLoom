import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.chat.corpus import copy_corpus_into
from app.db.session import system_session, tenant_session

# How long a visitor's throwaway tenant keeps its data.
DEMO_ORG_TTL = timedelta(hours=24)

# How long the token minted alongside it stays valid. Deliberately shorter than
# the organisation: a stolen demo token stops working long before the data it
# reaches is collected, and the visitor can log in again without losing it.
# Both numbers live here so the endpoint that reports one and the purge that
# enforces the other cannot drift apart.
DEMO_TOKEN_TTL_SECONDS = 3600


@dataclass(frozen=True)
class DemoOrganisation:
    id: str
    external_id: str
    name: str


async def provision_demo_org(label: str) -> DemoOrganisation:
    """Create a throwaway organisation for a visitor, holding the seed corpus.

    The corpus is not decoration. A visitor arrives owning nothing, and
    `search_chunks` is confined to what the caller's organisation owns, so
    without this the first question retrieves `[]`, the `citations` event is
    empty, and the demo's headline behaviour — sources rendering before the
    first token — quietly does not happen. Nothing raises, so nothing reports
    it. The corpus is copied rather than shared through a policy exception; see
    `app/chat/corpus.py` for why that trade is not available here.

    Both writes are one transaction, in the new organisation's own tenant
    context, so no demo organisation is ever committed without its corpus — a
    failure half way through leaves no organisation at all rather than an empty
    one that looks fine until the visitor asks something.

    The context can be the new organisation's because the id is generated here,
    before anything is written: `set_config` does not check that the
    organisation exists, and `organizations` has an unconditional INSERT policy
    (`WITH CHECK (true)`, migration 0003) precisely so a caller with no tenant
    yet can create one — an unscoped session was never the requirement, only a
    permissive INSERT policy was. The context is then already correct for the
    chunks, which `document_chunks` insists on: its WITH CHECK rejects a row
    stamped with any organisation other than the session's.

    A plain INSERT, with no ON CONFLICT clause. `external_id` is derived from a
    fresh uuid4 so a conflict cannot occur, and reaching for `ON CONFLICT` here
    would make things worse rather than safer: with an explicit conflict target
    Postgres runs the SELECT policy's qual as the conflict check, and without a
    target it does not error but silently no-ops against a colliding row in
    *any* tenant, which is an existence oracle for other organisations'
    external ids.
    """
    org_id = uuid.uuid4()
    external_id = f"demo_{org_id.hex}"
    async with tenant_session(str(org_id)) as session:
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
        await copy_corpus_into(session, str(org_id))
    return DemoOrganisation(id=str(org_id), external_id=external_id, name=label)


async def purge_expired_orgs() -> int:
    """Delete every organisation past its TTL, with all of its data. Returns
    the number of organisations removed.

    Deliberately one line: the entire purge is migration 0004's
    `public.purge_expired_orgs()`, and none of it belongs on this side.

    Two reasons it lives in the database. First, "which organisations have
    expired?" cannot be answered from any tenant context, and every Python-side
    shape that can answer it either hands an application role a list of
    organisation ids — which are the RLS credential, so that is an escalation
    primitive rather than a lookup — or needs a role that bypasses RLS, which
    the startup guard refuses to run as. Second, deciding here and deleting
    there splits one verdict across two transactions: an organisation could be
    renewed in between and be destroyed anyway on a verdict already stale. In
    the database it is one statement over one snapshot, with the expiring rows
    locked.

    Unscoped by definition — there is no tenant to scope to when the subject of
    the statement is every tenant that should no longer exist. `lexloom_app` is
    not granted EXECUTE, so in production only the worker reaches this.
    """
    async with system_session() as session:
        return await session.scalar(text("SELECT public.purge_expired_orgs()")) or 0
