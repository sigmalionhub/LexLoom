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
"""

import pytest
from httpx import AsyncClient

from sqlalchemy import text

from app.db.session import system_session
from app.ingest.catalogue import CATALOGUE
from app.observability import Registry
from test_ingest_job import (  # noqa: PLC2701
    SOURCE_KEY,
    _add_fake_ingested_chunk,
    _make_org,
)


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
    """A fresh demo org holds only the seed corpus — Task 22's SEED_SOURCE_KEY
    — which `read_ingest_status`'s rows-fallback does not count.
    """
    response = await client.get("/api/v1/ingest/status", headers=demo_auth)

    assert response.status_code == 200
    body = response.json()
    assert body["stage"] == "idle"
    assert body["chunks_done"] == 0
    assert body["chunks_total"] == 0
    assert body["error"] is None
    assert body["started_at"] is None


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
    assert body["stage"] == "queued"
    assert isinstance(body["started_at"], int)


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
