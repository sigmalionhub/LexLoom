import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app


@pytest.fixture
async def client() -> AsyncClient:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_liveness_returns_ok(client: AsyncClient) -> None:
    response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


async def test_readiness_reports_dependencies(client: AsyncClient) -> None:
    response = await client.get("/health/ready")
    assert response.status_code in (200, 503)
    body = response.json()
    assert set(body["checks"]) == {"database", "redis"}
