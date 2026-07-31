from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session as SyncSession

from app.auth.deps import CurrentPrincipal
from app.db.engine import session_factory


class MissingTenantContextError(RuntimeError):
    """A tenant-scoped session was opened without an organisation."""


@event.listens_for(SyncSession, "after_begin")
def _require_tenant_context(session: SyncSession, *_: object) -> None:
    """Structural guard: a session cannot begin without declaring its scope.

    System paths opt out explicitly and are greppable; everything else must
    carry an org_id, so forgetting the context is a crash, not a data leak.
    """
    if session.info.get("tenant_scoped", True) and not session.info.get("org_id"):
        raise MissingTenantContextError("session began without an organisation context")


@asynccontextmanager
async def tenant_session(org_id: str) -> AsyncIterator[AsyncSession]:
    """Open a session scoped to one organisation.

    set_config's third argument makes the setting transaction-local. A plain
    `SET` would persist for the pooled connection's whole life. `SET LOCAL`
    cannot take bind parameters, which is why people interpolate the token
    value into it — injecting attacker-controlled data straight into the
    security control. set_config parameterises.
    """
    if not org_id:
        raise MissingTenantContextError("org_id is required")
    session = session_factory(info={"org_id": org_id, "tenant_scoped": True})
    try:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"), {"org": org_id}
            )
            yield session
    finally:
        await session.close()


@asynccontextmanager
async def system_session() -> AsyncIterator[AsyncSession]:
    """Deliberately unscoped: migrations, the demo-org purge, health probes."""
    session = session_factory(info={"tenant_scoped": False})
    try:
        async with session.begin():
            yield session
    finally:
        await session.close()


async def get_tenant_session(principal: CurrentPrincipal) -> AsyncIterator[AsyncSession]:
    async with tenant_session(principal.org_id) as session:
        yield session


# The alias route handlers depend on. Never expose an unscoped session this way.
TenantSession = Annotated[AsyncSession, Depends(get_tenant_session)]
