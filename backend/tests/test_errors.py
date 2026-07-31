import pytest
from httpx import ASGITransport, AsyncClient

from app.errors import ProblemError
from app.main import create_app


@pytest.fixture
async def client() -> AsyncClient:
    app = create_app()

    @app.get("/boom")
    async def boom() -> None:
        raise ProblemError(
            code="document_too_large",
            status=413,
            title="Document exceeds size limit",
            detail="File is 84 MB, limit is 50 MB",
        )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


async def test_problem_response_shape(client: AsyncClient) -> None:
    response = await client.get("/boom")
    assert response.status_code == 413
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["code"] == "document_too_large"
    assert body["type"].endswith("/docs/errors#document-too-large")
    assert body["instance"] == "/boom"
    assert body["request_id"]


async def test_request_id_is_echoed_in_the_header(client: AsyncClient) -> None:
    response = await client.get("/boom")
    assert response.headers["x-request-id"] == response.json()["request_id"]
