"""`GET /api/v1/evals` publishes the committed measurement and computes nothing.

The last property is the one worth a test. "Serves a file" is not visible from
the outside — a handler that reseeded the corpus and measured it would return
the same shape — and it is the claim the residual-purge derivation beside this
route's entry in `test_demo_orgs.py::EXPECTED_ROUTES` rests on. It is also
exactly the kind of property a refactor loses silently: somebody imports
`app.evals.run` for a helper, the module pulls in a session, and one day the
handler is opening a transaction that nobody asked it to. So the SQL count is
asserted rather than described.
"""

import json

import pytest
from httpx import AsyncClient
from sqlalchemy import event

from app.api.v1.evals import RESULTS_FILE, _load_results
from app.db.engine import engine


@pytest.fixture(autouse=True)
def _forget_the_cached_artefact():
    """The handler caches for the life of the process; a test suite is one process."""
    _load_results.cache_clear()
    yield
    _load_results.cache_clear()


@pytest.fixture
def committed() -> dict:
    return json.loads(RESULTS_FILE.read_text(encoding="utf-8"))


async def test_the_endpoint_reports_the_numbers_in_the_committed_file(
    client: AsyncClient, committed: dict
) -> None:
    """Not "some numbers" — *these* numbers, every mode and every metric.

    Comparing the whole `modes` block rather than one cell is deliberate: a
    response model that dropped a field would still pass a spot check on
    `mrr_at_10`, and the panel would then render an empty column with no error
    anywhere.
    """
    response = await client.get("/api/v1/evals")

    assert response.status_code == 200
    body = response.json()
    assert body["modes"] == committed["modes"]
    assert body["corpus"] == committed["corpus"]
    assert body["questions"] == committed["questions"]
    assert body["provenance"] == committed["provenance"]
    assert body["measured_at"] == committed["measured_at"]
    assert body["commit"] == committed["commit"]
    # `schema`, spelled as it is in the file. Pydantic reserves the attribute
    # name, so the model calls it `schema_version` and aliases it back — and if
    # that alias were ever dropped the panel would read `undefined`.
    assert body["schema"] == committed["schema"]


async def test_the_endpoint_takes_no_token(client: AsyncClient) -> None:
    """There is nothing about a tenant in the response, so there is nothing for a
    token to have protected — the same argument `/api/v1/status` and
    `/api/v1/generators` make. Asserted because "public" is a decision, and a
    later `Depends(principal)` added out of habit would break the panel for every
    visitor who has not logged in yet."""
    assert (await client.get("/api/v1/evals")).status_code == 200


async def test_reading_the_evals_runs_no_sql(client: AsyncClient) -> None:
    """The claim the residual-purge derivation is written on.

    Counted on the engine rather than asserted about the handler's source, so
    the test still holds when the reason it could break is two imports away.
    """
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):  # noqa: PLR0913
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        response = await client.get("/api/v1/evals")
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)

    assert response.status_code == 200
    assert statements == [], (
        "GET /api/v1/evals executed SQL. It is documented as serving a stored file, and "
        "the purge's residual argument in test_demo_orgs.py is derived from that — a "
        "handler that reaches the database has to have that argument derived again."
    )


async def test_the_artefact_is_read_once_per_process_not_once_per_request(
    client: AsyncClient,
) -> None:
    """Ninety searches and thirty ONNX inferences per request would be a
    denial-of-service lever needing no authentication; so would re-reading and
    re-parsing the file. The cache is what makes the response a constant in
    memory, which is also why this route needs no admission slot."""
    for _ in range(3):
        assert (await client.get("/api/v1/evals")).status_code == 200

    info = _load_results.cache_info()
    assert info.misses == 1
    assert info.hits == 2


async def test_a_missing_artefact_is_a_503_problem_and_never_a_page_of_zeros(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A build without the file is incomplete, not a service whose retrieval scores
    zero. The panel renders the difference, so the API has to make it."""
    monkeypatch.setattr(
        "app.api.v1.evals.RESULTS_FILE", RESULTS_FILE.with_name("results.absent.json")
    )
    _load_results.cache_clear()

    response = await client.get("/api/v1/evals")

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    problem = response.json()
    assert problem["code"] == "evals_unavailable"
    assert problem["status"] == 503
    assert "results.json" in problem["detail"]


# Nothing here asserts that the router is mounted, deliberately: `app/main.py`
# includes routers explicitly, so a forgotten `include_router` turns every test
# above into a 404, and `test_demo_orgs.py::test_the_route_surface_is_the_one_
# the_residual_argument_was_written_for` inventories the mounted surface by
# name. A third assertion of the same fact would only be a third place to
# update.
