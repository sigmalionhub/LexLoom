"""`GET /api/v1/status` and the metrics registry behind it.

Three concerns, and they map onto the three risks `task-25-brief.md` names:

1. The timer in `RequestContextMiddleware` has to stop at `http.response.start`,
   not at the end of `await self.app(...)` — otherwise every SSE stream is
   timed as if its whole duration were request latency.
2. The percentile function has to interpolate inside a bucket and has to
   refuse to report a value past the top edge as the top edge.
3. The public payload has to carry exactly the keys it is supposed to, and
   nothing that discloses ceiling occupancy.
"""

import asyncio

import pytest
from httpx import AsyncClient

from app.middleware import RequestContextMiddleware
from app.observability import Registry


@pytest.fixture
def reset_registry(monkeypatch: pytest.MonkeyPatch):
    """Substitute a fresh `Registry` for `app.observability._registry`.

    Returned as a zero-argument callable rather than the registry itself, so a
    test can call it after describing the scope it wants measured in — and so
    two calls in one test (there are none today) would each start clean.
    """

    def _reset() -> Registry:
        fresh = Registry()
        monkeypatch.setattr("app.observability._registry", fresh)
        return fresh

    return _reset


# ---------------------------------------------------------------------------
# Step 5: percentile interpolation, and the overflow bucket that must not be
# reported as its own edge.
# ---------------------------------------------------------------------------


def test_percentiles_interpolate_inside_the_bucket() -> None:
    """A known distribution with arithmetic that only one implementation produces.

    `assert p95 > 0` holds for every implementation ever written, including one
    that returns the bucket edge, one that returns the mean and one that
    returns the number 1. The figures below are the interpolated positions and
    nothing else: 70 samples in (2, 5] and 30 in (25, 50] put the median a
    third of the way into the first bucket and p95 five sixths into the
    second.
    """
    registry = Registry()
    for _ in range(70):
        registry.record_response_start("/api/v1/conversations", 200, 3.0)
    for _ in range(30):
        registry.record_response_start("/api/v1/conversations", 200, 40.0)

    p = registry.snapshot().time_to_response_start
    assert (p.p50_ms, p.p95_ms, p.p99_ms) == (4, 46, 49)
    assert p.samples == 100 and p.above_count == 0


def test_a_sample_past_the_top_bucket_is_not_reported_as_the_top_bucket() -> None:
    """Ten nine-second samples against the request ladder, which stops at 5000 ms.

    This is where revision 1 put every stream. A percentile that answers
    `5000` here reads as "the slowest request took five seconds", when what is
    actually known is "over five, and this ladder cannot see how much over".
    """
    registry = Registry()
    for _ in range(10):
        registry.record_response_start("/api/v1/conversations", 200, 9000.0)

    p = registry.snapshot().time_to_response_start
    assert (p.p50_ms, p.p95_ms, p.p99_ms) == (None, None, None)
    assert (p.above_ms, p.above_count) == (5000, 10)


def test_an_idle_window_reports_no_measurement_rather_than_zero() -> None:
    p = Registry().snapshot()
    assert p.requests.total == 0
    assert p.time_to_response_start.p50_ms is None
    assert p.time_to_response_start.samples == 0


# ---------------------------------------------------------------------------
# Step 3: the classification heuristic — path suffix, not route name — is
# exercised through the actual route surface, not just asserted about a
# string.
# ---------------------------------------------------------------------------


def test_exactly_one_route_is_classified_as_a_stream(app) -> None:
    """The middleware tells a stream from a request by path suffix, and that is
    a claim about the route surface, not about a string.

    A second streaming route added later would be timed by the request ladder
    if it did not end in `/stream`, and every ordinary route ending in
    `/stream` would be dropped out of the request ladder into the stream one.
    Either way the panel's headline number would change meaning with no test
    going red — which is the failure this whole task exists to fix, arriving
    through a different door.

    Walked through `_IncludedRouter.original_router`, not `route.path` on
    `app.routes` directly: since FastAPI 0.13x an `include_router` call leaves
    one of these, and its children hang off `original_router.routes` rather
    than off `routes` or `path` — see the fuller walker (and its Mount/Host
    handling, unneeded here because this app has none) in
    `test_demo_orgs.py::_route_surface`.
    """

    def _paths(routes) -> list[str]:
        found: list[str] = []
        for route in routes:
            included = getattr(route, "original_router", None)
            children = included.routes if included is not None else getattr(route, "routes", None)
            if children is not None:
                found.extend(_paths(children))
                continue
            path = getattr(route, "path", None)
            if path is not None:
                found.append(path)
        return found

    streaming = sorted(path for path in _paths(app.routes) if path.endswith("/stream"))
    assert streaming == ["/api/v1/conversations/{conversation_id}/messages/{message_id}/stream"]


def test_the_status_endpoint_itself_is_excluded_from_the_ladder(reset_registry) -> None:
    """§3.1: a poller that measures mostly its own polling is not observability."""
    registry = reset_registry()
    registry.record_response_start("/api/v1/status", 200, 1.0)
    assert registry.snapshot().time_to_response_start.samples == 0


def test_the_ingest_status_route_is_excluded_before_it_exists(reset_registry) -> None:
    """§3.3: `/api/v1/ingest/status` is not a prefix of `/api/v1/status`, and
    `startswith` does not cover it either way — this is the literal Task 24
    depends on this branch to have written in advance.
    """
    registry = reset_registry()
    registry.record_response_start("/api/v1/ingest/status", 200, 1.0)
    assert registry.snapshot().time_to_response_start.samples == 0


def test_health_probes_are_excluded(reset_registry) -> None:
    registry = reset_registry()
    registry.record_response_start("/health/ready", 200, 1.0)
    registry.record_response_start("/health/live", 200, 1.0)
    assert registry.snapshot().time_to_response_start.samples == 0


# ---------------------------------------------------------------------------
# Step 6: the timer really does stop at `http.response.start`, exercised
# through the raw ASGI middleware rather than through the registry directly —
# this is the test that catches the regression `send_with_header` exists to
# prevent.
# ---------------------------------------------------------------------------


async def _no_receive() -> dict:
    raise AssertionError("the app under test never awaits receive()")


async def _collect(message: dict) -> None:
    return None


async def slow_body(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await asyncio.sleep(0.3)
    await send({"type": "http.response.body", "body": b"done"})


async def test_the_histogram_stops_at_the_response_start_not_at_the_end_of_the_body(
    reset_registry,
) -> None:
    """A response whose headers are immediate and whose body takes 300 ms.

    This is the shape of every SSE stream and of nothing else in the
    application, which is why revision 1's histogram measured the wrong thing
    without a single test going red: the middleware returns when the
    generator is exhausted.
    """
    registry = reset_registry()
    await RequestContextMiddleware(slow_body)(
        {"type": "http", "path": "/api/v1/conversations"}, _no_receive, _collect
    )

    p = registry.snapshot().time_to_response_start
    assert p.samples == 1
    assert p.p50_ms is not None and p.p50_ms < 50, (
        "the recorded figure is the body's duration, not the time to the status line"
    )


async def test_a_stream_is_measured_as_a_stream_and_stays_out_of_the_request_ladder(
    reset_registry,
) -> None:
    registry = reset_registry()
    await RequestContextMiddleware(slow_body)(
        {"type": "http", "path": "/api/v1/conversations/c/messages/m/stream"},
        _no_receive,
        _collect,
    )

    snapshot = registry.snapshot()
    assert snapshot.time_to_response_start.samples == 0
    assert snapshot.stream_duration.samples == 1


async def test_a_response_with_no_status_line_is_not_recorded_as_a_fast_stream(
    reset_registry,
) -> None:
    """`started_responding` guards a stream whose generator never sends
    `http.response.start` at all — an ASGI-level crash before headers.
    Recording that as a fast stream would be an invention.
    """

    async def crashes_before_headers(scope, receive, send):
        return None

    registry = reset_registry()
    await RequestContextMiddleware(crashes_before_headers)(
        {"type": "http", "path": "/api/v1/conversations/c/messages/m/stream"},
        _no_receive,
        _collect,
    )

    snapshot = registry.snapshot()
    assert snapshot.stream_duration.samples == 0
    assert snapshot.time_to_response_start.samples == 0


# ---------------------------------------------------------------------------
# Step 7: the payload inventory. This is the test that would have caught
# revision 1's `streams: {active, capacity}`.
# ---------------------------------------------------------------------------

# Every key the public payload may carry, at every level. A literal for the
# same reason EXPECTED_ROUTES is one: a field added to a public response has
# to be a decision somebody made, not a line that appeared in a model.
PUBLISHED_FIELDS = {
    "commit",
    "uptime_seconds",
    "window_seconds",
    "requests",
    "total",
    "by_class",
    "time_to_response_start",
    "stream_duration",
    "p50_ms",
    "p95_ms",
    "p99_ms",
    "samples",
    "above_ms",
    "above_count",
}

WITHHELD = ("active", "capacity", "limit", "tenants", "org", "slot", "queue", "redis", "pool")


def _keys_at_every_level(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            keys.add(key)
            keys |= _keys_at_every_level(item)
    elif isinstance(value, list):
        for item in value:
            keys |= _keys_at_every_level(item)
    return keys


async def test_the_status_payload_discloses_nothing_about_the_ceilings(
    client: AsyncClient, reset_registry
) -> None:
    """`limits.py:227-231` refuses to say what the limit is or how much of it
    is spent, because both are information about the ceiling. An endpoint that
    publishes occupancy hands back what the 429 body withholds, to anyone, for
    free, with no login and nothing in the log.

    `reset_registry` isolates this from every other test in the suite: without
    it, `requests.by_class` would carry real `"2xx"`/`"4xx"` labels left over
    from whatever else the process has served, and those are legitimate
    *values* of `by_class`, not undeclared *fields* — a key walker cannot
    tell the two apart, so the fixture is what keeps the distinction rather
    than the walker.
    """
    reset_registry()
    response = await client.get("/api/v1/status")
    body = response.json()
    keys = _keys_at_every_level(body)
    undeclared = keys - PUBLISHED_FIELDS
    assert keys <= PUBLISHED_FIELDS, f"undeclared fields in a public payload: {undeclared}"
    assert not [k for k in keys if any(word in k for word in WITHHELD)]


async def test_the_status_endpoint_requires_no_authentication(client: AsyncClient) -> None:
    """Part of why this route is not an exception to the residual-purge
    argument: it takes no token at all.
    """
    response = await client.get("/api/v1/status")
    assert response.status_code == 200


async def test_the_status_response_matches_its_declared_shape(client: AsyncClient) -> None:
    response = await client.get("/api/v1/status")
    body = response.json()
    assert set(body) == {
        "commit",
        "uptime_seconds",
        "window_seconds",
        "requests",
        "time_to_response_start",
        "stream_duration",
    }
    assert body["window_seconds"] == 300
    assert set(body["requests"]) == {"total", "by_class"}
    for bucket in ("time_to_response_start", "stream_duration"):
        assert set(body[bucket]) == {
            "p50_ms",
            "p95_ms",
            "p99_ms",
            "samples",
            "above_ms",
            "above_count",
        }
