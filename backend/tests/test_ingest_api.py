"""The HTTP surface over `app.ingest.jobs`: `POST /api/v1/ingest` and
`GET /api/v1/ingest/status`.

Job internals — both ceilings, the progress hash, the guarded write, the real
model against the full catalogue source — are `test_ingest_job.py`'s job, not
this file's. Every test here dispatches through a monkeypatched
`ingest_source.delay`, never the real task, so this module never touches the
embedding model or waits on a Celery worker: the round trip it tests is
route -> `app.ingest.jobs` -> the dispatch call, and the shape of what comes
back.

That monkeypatch is also why nothing here carries `@pytest.mark.model` — the
one path in this file that could reach the model (a real `.delay()` running a
real ingest) is exactly the one every test below replaces.

One test breaks the "never the real task" rule and says so where it lives:
`test_a_failed_ingest_returns_a_code_and_leaks_no_sql_or_document_text` needs
the value the task's own `except` clause writes and the value this route reads
back, so stubbing either half would prove nothing. It stubs the *work* instead
— no PDF, no weights — and stays model-free like the rest.
"""

import asyncio

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_ingest_job import (  # noqa: PLC2701
    PRELOADED_KEY,
    SOURCE_KEY,
    _add_fake_ingested_chunk,
    _make_org,
)

from app.db.session import system_session
from app.ingest.catalogue import CATALOGUE
from app.ingest.preload import PRELOADED_SOURCE_KEYS
from app.observability import Registry


@pytest.fixture
def dispatched(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Replace the real Celery dispatch with a recorder.

    Patched on `app.worker.tasks.ingest_source`, the module `app.api.v1.ingest`
    imports its `ingest_source` reference from — the same task singleton, so
    the route sees the fake `.delay` too.
    """
    calls: list[tuple[str, str]] = []

    def _fake_delay(org_id: str, source_key: str) -> None:
        calls.append((org_id, source_key))

    monkeypatch.setattr("app.worker.tasks.ingest_source.delay", _fake_delay)
    return calls


# --- POST /api/v1/ingest -----------------------------------------------------


async def test_post_ingest_dispatches_and_returns_the_initial_status_snapshot(
    client: AsyncClient,
    demo_auth: dict[str, str],
    demo_org_id: str,
    dispatched: list[tuple[str, str]],
    settings_override,
) -> None:
    from conftest import LIMITS_TEST_REDIS_URL

    settings_override(redis_url=LIMITS_TEST_REDIS_URL)

    response = await client.post(
        "/api/v1/ingest", json={"source_key": SOURCE_KEY}, headers=demo_auth
    )

    assert response.status_code == 202
    body = response.json()
    assert body["stage"] == "queued"
    assert body["chunks_done"] == 0
    assert body["chunks_total"] == 0
    assert body["error"] is None
    assert isinstance(body["started_at"], int)

    # Dispatched exactly once, for this organisation and this source — not a
    # job id in sight, per Step 2: the response above is the same shape
    # `GET .../status` returns, not `{job_id: ...}`.
    assert dispatched == [(demo_org_id, SOURCE_KEY)]


async def test_post_ingest_requires_authentication(client: AsyncClient) -> None:
    response = await client.post("/api/v1/ingest", json={"source_key": SOURCE_KEY})
    assert response.status_code == 401
    assert response.json()["code"] == "missing_credentials"


async def test_post_ingest_404s_for_a_source_key_outside_the_catalogue(
    client: AsyncClient, demo_auth: dict[str, str], dispatched: list[tuple[str, str]]
) -> None:
    assert "not-a-real-source" not in CATALOGUE  # the premise of this test

    response = await client.post(
        "/api/v1/ingest", json={"source_key": "not-a-real-source"}, headers=demo_auth
    )

    assert response.status_code == 404
    assert response.json()["code"] == "source_not_found"
    assert dispatched == []  # refused before admission is ever consulted


async def test_post_ingest_409s_when_this_organisation_already_holds_a_corpus(
    client: AsyncClient,
    demo_auth: dict[str, str],
    demo_org_id: str,
    dispatched: list[tuple[str, str]],
) -> None:
    """Task 23's per-organisation ceiling, seen from the API. A second ingest
    by the same organisation must answer `409`, not start a second run —
    Cross-branch fact #7: this is the full path, so it is checked here.
    """
    await _add_fake_ingested_chunk(demo_org_id)

    response = await client.post(
        "/api/v1/ingest", json={"source_key": SOURCE_KEY}, headers=demo_auth
    )

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "already_ingested"
    # No numbers in the detail — limits.py:226-231's rule extended to this
    # ceiling, same as `capacity_unavailable` below.
    assert not any(ch.isdigit() for ch in body["detail"])
    assert dispatched == []  # no second run was ever queued


async def test_post_ingest_503s_with_retry_after_when_the_global_cap_is_held(
    client: AsyncClient,
    demo_auth: dict[str, str],
    dispatched: list[tuple[str, str]],
    settings_override,
    created_orgs: list[str],
) -> None:
    """Same technique as `test_ingest_job.py`'s global-cap test: measured as a
    delta against the count taken before seeding, because the shared dev
    database is long-lived and an absolute number would be flaky against
    whatever another concurrent run holds. A second, unrelated organisation
    is pushed over a cap set to exactly what is currently held, then the
    organisation actually under test — fresh, holding only the seed corpus —
    is refused by the *global* ceiling rather than the per-organisation one.
    """
    async with system_session() as session:
        before = await session.scalar(text("SELECT public.count_orgs_with_corpus()"))

    held_org = await _make_org(created_orgs)
    await _add_fake_ingested_chunk(held_org)
    settings_override(ingest_org_cap=before + 1)

    response = await client.post(
        "/api/v1/ingest", json={"source_key": SOURCE_KEY}, headers=demo_auth
    )

    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "capacity_unavailable"
    assert response.headers["retry-after"] == "300"
    assert not any(ch.isdigit() for ch in body["detail"])
    assert dispatched == []


# --- GET /api/v1/ingest/status ------------------------------------------------


async def test_get_ingest_status_reports_idle_before_any_ingest(
    client: AsyncClient, demo_auth: dict[str, str]
) -> None:
    """A fresh demo org has ingested nothing, and says so per source.

    "Nothing ingested" is no longer "nothing held". Provisioning gives every
    tenant the seeded excerpts *and* the whole of `gdpr-full`, so the map is
    read one source at a time: the preloaded document reports `complete` off its
    own rows, and the one nobody has asked for reports `idle`. A test that
    asserted every entry was idle would have to be satisfied by a deployment
    that never preloaded anything.
    """
    response = await client.get("/api/v1/ingest/status", headers=demo_auth)

    assert response.status_code == 200
    body = response.json()
    # A map now, one entry per catalogue source; see the route's docstring.
    assert set(body) == set(CATALOGUE)
    assert body[PRELOADED_KEY]["stage"] == "complete"
    assert body[PRELOADED_KEY]["chunks_done"] == CATALOGUE[PRELOADED_KEY].expected_chunks
    assert body[SOURCE_KEY]["stage"] == "idle"
    assert body[SOURCE_KEY]["chunks_done"] == 0
    assert body[SOURCE_KEY]["chunks_total"] == 0
    assert body[SOURCE_KEY]["error"] is None
    assert body[SOURCE_KEY]["started_at"] is None


async def test_get_ingest_status_requires_authentication(client: AsyncClient) -> None:
    response = await client.get("/api/v1/ingest/status")
    assert response.status_code == 401
    assert response.json()["code"] == "missing_credentials"


async def test_get_ingest_status_reflects_a_queued_post(
    client: AsyncClient,
    demo_auth: dict[str, str],
    dispatched: list[tuple[str, str]],
    settings_override,
) -> None:
    from conftest import LIMITS_TEST_REDIS_URL

    settings_override(redis_url=LIMITS_TEST_REDIS_URL)

    await client.post("/api/v1/ingest", json={"source_key": SOURCE_KEY}, headers=demo_auth)
    response = await client.get("/api/v1/ingest/status", headers=demo_auth)

    assert response.status_code == 200
    body = response.json()
    assert body[SOURCE_KEY]["stage"] == "queued"
    assert isinstance(body[SOURCE_KEY]["started_at"], int)
    # The other source is the preloaded one, and it reports what this tenant
    # actually holds rather than being absent or being dragged along by this
    # source's progress hash — the defect the per-source key exists to prevent.
    assert body[PRELOADED_KEY]["stage"] == "complete"


# --- Step 3: the metrics exception this task depends on, checked rather than
# trusted ----------------------------------------------------------------------


async def test_the_polled_status_route_is_not_in_the_request_histogram(
    client: AsyncClient, demo_auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The panel polls this twice a second. Counted, it would become most of the
    sample and the headline p50 would describe the poller, not the service.

    Asserted here rather than trusted: the literal (`_NOT_MEASURED`) lives in
    another branch (Task 25), and a prefix that looks like it covers this path
    (`/api/v1/status`) does not — `/api/v1/status` is not a string-prefix of
    `/api/v1/ingest/status`. This test drives a real request through the
    mounted route and the real `RequestContextMiddleware`, rather than calling
    `record_response_start` directly, so it would fail on a route mounted at a
    different path from the one the literal names, not only on the literal
    itself being edited.

    If this is red: that is a cross-branch discrepancy between this branch (D)
    and the branch owning `_NOT_MEASURED` (C) — escalate it, do not edit the
    literal here (see the brief, cross-branch fact #1).
    """
    fresh = Registry()
    monkeypatch.setattr("app.observability._registry", fresh)

    response = await client.get("/api/v1/ingest/status", headers=demo_auth)
    assert response.status_code == 200

    snapshot = fresh.snapshot()
    assert snapshot.requests.total == 0
    assert snapshot.time_to_response_start.samples == 0


async def test_a_route_that_is_not_excluded_is_still_counted(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control case for the test above: without it, a `Registry` that
    (say) excluded everything by mistake would pass the assertion above for
    the wrong reason.
    """
    fresh = Registry()
    monkeypatch.setattr("app.observability._registry", fresh)

    response = await client.get("/api/v1/generators")
    assert response.status_code == 200

    snapshot = fresh.snapshot()
    assert snapshot.requests.total == 1


# --- What a failed ingest tells the visitor ----------------------------------
#
# The exception to this module's "never the real task" rule, and it is the
# point of the test: the value under test is written by
# `app.worker.tasks.ingest_source`'s own `except` clause and read back through
# the route, so replacing either half would prove nothing. The real task body
# runs here, but not the real work — the parse is stubbed to two tiny chunks
# and the embedder to a three-float stub, so the INSERT fails on the vector
# width without a PDF, without the ONNX weights and in milliseconds.
#
# A wrong-width vector is not an arbitrary way to make it fail. It reproduces
# the exact shape the reviewer measured: SQLAlchemy raises a `DBAPIError` whose
# `str()` is the failing statement plus `[parameters: ...]`, which is how 7,314
# characters of `INSERT INTO document_chunks …`, document text and embedding
# floats reached the client through this route.

_LEAKY_CONTENT = "Article 28 obligations of the processor, verbatim from the document"


class _ThreeFloatModel:
    """Not `DIMENSIONS` wide, which is the whole idea — the INSERT then fails
    inside the write transaction with a real, statement-bearing database
    error."""

    def embed(self, texts):
        class _Row(list):
            def tolist(self):
                return list(self)

        return [_Row([0.1, 0.2, 0.3]) for _ in texts]


@pytest.fixture
def failing_ingest(monkeypatch: pytest.MonkeyPatch):
    import app.chat.embedding as embedding_module
    from app.ingest.parse import Chunk

    def _fake_parse(source):
        return [
            Chunk(chunk_ordinal=0, content=_LEAKY_CONTENT, page_from=1, page_to=1),
            Chunk(chunk_ordinal=1, content=_LEAKY_CONTENT, page_from=1, page_to=2),
        ]

    monkeypatch.setattr("app.ingest.parse.parse_source", _fake_parse)
    monkeypatch.setattr(embedding_module, "_model", _ThreeFloatModel())


async def test_a_failed_ingest_returns_a_code_and_leaks_no_sql_or_document_text(
    client: AsyncClient,
    demo_auth: dict[str, str],
    demo_org_id: str,
    failing_ingest,
    settings_override,
) -> None:
    """Sabotage: put `str(exc)` back into `_mark_error` and every assertion
    below fails at once — the response body then contains the INSERT, the bound
    parameters and `_LEAKY_CONTENT`."""
    from conftest import LIMITS_TEST_REDIS_URL

    from app.db.engine import engine
    from app.worker.tasks import ingest_source

    settings_override(redis_url=LIMITS_TEST_REDIS_URL)

    # In a thread, because the task body is synchronous by construction: it
    # opens its own loop with `asyncio.run`, which cannot be called from the
    # one this test is already running on. That is the worker's real shape, so
    # the thread is how the test meets it rather than a way around it.
    #
    # Disposed first, and this line is load-bearing rather than hygiene: the
    # engine's pool holds connections created on *this* test's loop, and the
    # task's own loop would fail on the first of them with "attached to a
    # different loop" — before ever reaching the INSERT. The test would still
    # go red under sabotage, but for a failure that carries no SQL, so it would
    # be proving nothing about the thing it is named for. Verified by putting
    # `str(exc)` back and reading which exception arrived.
    await engine.dispose()
    result = await asyncio.to_thread(ingest_source.apply, args=[demo_org_id, SOURCE_KEY])
    assert result.failed(), "the fixture did not actually make the ingest fail"
    assert isinstance(result.result, DBAPIError), (
        "the ingest failed, but not at the INSERT — this test only proves what it "
        f"claims if the failure is a statement-bearing database error, not {result.result!r}"
    )

    response = await client.get("/api/v1/ingest/status", headers=demo_auth)
    assert response.status_code == 200
    body = response.json()
    assert body[SOURCE_KEY]["stage"] == "error"
    assert body[SOURCE_KEY]["error"] == "ingest_failed"

    # Asserted on the whole serialised response, not on the field, because the
    # point is that nothing anywhere in what the visitor receives carries it.
    raw = response.text
    for leaked in ("INSERT INTO", "document_chunks", "parameters:", _LEAKY_CONTENT):
        assert leaked not in raw, f"the ingest error still leaks {leaked!r}"
    # A code, not a paragraph: the value also goes into a 48 MB allkeys-lru
    # Redis, where multi-kilobyte writes evict the rate-limit counters.
    assert len(body[SOURCE_KEY]["error"]) < 64


# --- The catalogue route ------------------------------------------------------


async def test_the_catalogue_lists_every_source_and_nothing_about_the_files(
    client: AsyncClient,
) -> None:
    """Seven fields, and the ones deliberately absent.

    `Source` also carries `path` (an absolute path inside the image), `sha256`,
    `document_xml_sha256` and `conversion` (which names the build tool and its
    exact version). A response model that mirrored the dataclass would have
    published all four; `CatalogueEntry` is what stops it, so the absence is
    asserted rather than trusted.

    The set is pinned rather than sampled, which is why adding `suggestions`
    failed here first. That is the test working: a field reaches the public
    catalogue only when someone writes it down here.
    """
    response = await client.get("/api/v1/catalogue")
    assert response.status_code == 200

    body = response.json()
    assert [entry["key"] for entry in body] == list(CATALOGUE)
    for entry in body:
        assert set(entry) == {
            "key",
            "title",
            "media_type",
            "download",
            "source_url",
            "preloaded",
            "suggestions",
        }
        assert entry["preloaded"] == (entry["key"] in PRELOADED_SOURCE_KEYS)
        # The file name, never the path it sits at inside the image. `download`
        # is what the static export serves the document under; the directory is
        # the part that stays on this side, and asserting the difference is what
        # keeps a later `download=str(source.path)` from shipping it.
        source = CATALOGUE[entry["key"]]
        assert entry["download"] == source.path.name
        assert "/" not in entry["download"]
        assert entry["source_url"] == source.source_url
        # Carried through as sent, not reordered or deduplicated: the page
        # renders them in this order and `test_suggested_questions.py` verifies
        # them in it.
        assert entry["suggestions"] == list(CATALOGUE[entry["key"]].suggestions)

    serialised = response.text
    for leaked in ("/app/", "sha256", "LibreOffice", "build_docx_source"):
        assert leaked not in serialised


async def test_the_catalogue_needs_no_token(client: AsyncClient) -> None:
    """Unauthenticated on purpose — see the route's docstring and its entry in
    `EXPECTED_ROUTES`. Asserted because "public" is a security decision, and a
    decision nothing checks is a decision that can be reversed by accident."""
    response = await client.get("/api/v1/catalogue")
    assert response.status_code == 200
