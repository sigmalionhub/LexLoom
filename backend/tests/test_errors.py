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

    @app.get("/validate")
    async def validate(count: int) -> dict[str, int]:
        return {"count": count}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def unraising_client() -> AsyncClient:
    """A client that does not re-raise the app's unhandled exceptions.

    Starlette's ServerErrorMiddleware always re-raises after sending its
    response — so a real ASGI server still gets to log it — and httpx's
    ASGITransport propagates that into the caller by default. Without
    `raise_app_exceptions=False` this test would fail on the re-raised
    RuntimeError instead of asserting on the response the handler produced.
    """
    app = create_app()

    @app.get("/boom-unhandled")
    async def boom_unhandled() -> None:
        raise RuntimeError("super secret internal detail: db password is hunter2")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
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


async def test_unhandled_exception_becomes_problem_json(unraising_client: AsyncClient) -> None:
    response = await unraising_client.get("/boom-unhandled")
    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["code"] == "internal_error"
    assert body["request_id"]
    assert response.headers["x-request-id"] == body["request_id"]


async def test_unhandled_exception_does_not_leak_internals(unraising_client: AsyncClient) -> None:
    response = await unraising_client.get("/boom-unhandled")
    body = response.json()
    assert "hunter2" not in body["detail"]
    assert "secret" not in body["detail"]


async def test_unknown_path_is_problem_json_not_found(client: AsyncClient) -> None:
    response = await client.get("/this-path-does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "not_found"


async def test_validation_failure_is_problem_json(client: AsyncClient) -> None:
    response = await client.get("/validate")
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "validation_error"
