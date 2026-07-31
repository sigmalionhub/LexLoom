import os

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://lexloom_app:app@localhost:5433/lexloom"
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("ENV", "ci")

from app.db.session import tenant_session  # noqa: E402
from app.main import create_app  # noqa: E402
from app.worker.celery_app import celery_app  # noqa: E402


@pytest.fixture
def app() -> FastAPI:
    """Exposed separately so tests can reach app.state (the dev IdP, the JWKS
    cache) without poking at private attributes on the HTTP client."""
    return create_app()


@pytest.fixture
async def client(app: FastAPI) -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest.fixture
async def created_orgs():
    """Registry of organisations a test seeded, emptied on the way out.

    The development database is long-lived and shared between runs, so a test
    that seeds a tenant and walks away changes what the *next* run measures.
    That is not hypothetical here: the demo organisations this task introduces
    carry an expiry, so yesterday's leftovers are today's already-expired rows
    and any purge count computed over them is ambient rather than specific.

    Removal goes through each organisation's own tenant_session — the only
    context its RLS policies admit — and takes the child rows first, because
    this schema has no foreign keys and therefore no cascade.
    """
    registry: list[str] = []
    yield registry
    for org_id in registry:
        async with tenant_session(org_id) as session:
            for statement in (
                "DELETE FROM messages WHERE org_id = :org",
                "DELETE FROM document_chunks WHERE org_id = :org",
                "DELETE FROM conversations WHERE org_id = :org",
            ):
                await session.execute(text(statement), {"org": org_id})
            await session.execute(
                text("DELETE FROM organizations WHERE id = :org"), {"org": org_id}
            )


@pytest.fixture
async def demo_auth(client: AsyncClient, created_orgs: list[str]) -> dict[str, str]:
    """Log in as a throwaway organisation and return the Authorization header."""
    body = (
        await client.post("/api/v1/auth/demo-login", json={"label": "Acme Legal"})
    ).json()
    created_orgs.append(body["org_id"])
    return {"Authorization": f"Bearer {body['access_token']}"}


@pytest.fixture
def celery_eager():
    celery_app.conf.task_always_eager = True
    yield
    celery_app.conf.task_always_eager = False


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    """Each test gets its own event loop; the engine's pool must not outlive it.

    A connection created on a previous test's loop and handed to the next one
    fails in ways that point at the wrong code entirely.
    """
    yield
    from app.db.engine import engine

    await engine.dispose()
