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


async def test_readiness_is_unhealthy_when_one_dependency_is_down(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """200 requires both dependencies. One down must be 503, not a partial OK.

    Both checks are monkeypatched — including the one left `True` — rather
    than exercising the real database. This is a unit test of the response
    composition logic in `readiness()`; real connectivity is already covered
    by `test_readiness_reports_dependencies` and `test_db_role_guard.py`. It
    also sidesteps a real cross-event-loop pitfall: the module-level `engine`
    singleton pools connections across pytest-asyncio's per-test event loops,
    and a second test taking the real `_database_ok()` path here reliably
    produced `RuntimeWarning: coroutine 'Connection._cancel' was never
    awaited` from a connection object handed out on one test's loop after
    that loop had already closed.
    """
    from app.api.v1 import health

    async def _true() -> bool:
        return True

    async def _false() -> bool:
        return False

    monkeypatch.setattr(health, "_database_ok", _true)
    monkeypatch.setattr(health, "_redis_ok", _false)
    response = await client.get("/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["checks"] == {"database": True, "redis": False}
    assert body["status"] == "degraded"
