"""In-process request and stream metrics, and the public status payload.

Everything here lives in the memory of one uvicorn worker (`--workers 1`,
`backend/Dockerfile:27`), not in Redis. `app/limits.py` explains at length why
its ceilings have to live in a shared backend; this module is the opposite
case — a number that is allowed to mean "this process" rather than "the
fleet", so it does not have to pay Redis's cost per request. See
`docs/api-contract.md` and the comment beside `Registry` for the full
argument. If this service ever runs more than one worker, the honest fixes are
either labelling these figures per-process or moving them to Redis then; adding
Redis now would pay that cost for a property this deployment does not have.
"""

import bisect
import os
import re
import time
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

# Fixed buckets and one counter each, not a list of samples: a list would be
# exact and would also grow with request volume, which is the one input
# nobody here controls.
#
# Two ladders because one cannot serve both classes. The request ladder has to
# resolve single milliseconds; the stream ladder has to resolve tens of
# seconds. A single ladder covering both would put every ordinary request in
# its first bucket.
#
# The only measurement of `gemini-3.6-flash` this repository holds is
# 9.52-11.98 s end to end for a long answer (350-510 words), 7.56-8.91 s to
# first token — three runs per model, taken locally on 2026-08-04 and
# published in `frontend/components/panel/generator-selector.tsx` as
# "~9.5-12.0s". This comment previously said 45 s "was measured"; nothing
# measured 45 s. The figure came from an illustrative selector mock-up in the
# milestone plan ("flash — 12–45 s") and was a guess. The ladder is unchanged,
# because it was never sized on that number: 10 s and 20 s are the two buckets
# the measured range actually straddles, and 30/45/60/120 s are tail buckets —
# a slow network, a long answer on the deploy box, a retry — where the point is
# to see *that* a stream took that long, not to resolve how long.
REQUEST_BUCKETS_MS = (1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000)
STREAM_BUCKETS_MS = (500, 1000, 2500, 5000, 10_000, 20_000, 30_000, 45_000, 60_000, 120_000)

# Machine polls at a fixed interval: counted, they measure the poller's period
# rather than the service's behaviour, and they pull p50 towards the cheapest
# route in the application.
#
# `/api/v1/ingest/status` was listed here before it existed, on purpose: a
# literal that has to be edited by the task that adds a route is a literal that
# gets forgotten by the task that adds the route, so the branch that owned this
# rule owned the whole list. The route has since arrived, and this note is kept
# only to record why the entry predates it — the entry itself is now an
# ordinary one. Its panel polls it twice a second, which makes it the most
# aggressive poller in the product and the strongest case on this list for not
# being counted.
_NOT_MEASURED = ("/api/v1/status", "/api/v1/ingest/status", "/health/")
_STREAM_SUFFIX = "/stream"

_WINDOW_SECONDS = 60
_WINDOWS = 5  # the payload reports five minutes; the ring holds one spare slot


@dataclass
class _Slot:
    """One minute of counters. Reset in place when its minute comes round again."""

    minute: int = -1
    request_ms: list[int] = field(default_factory=lambda: [0] * (len(REQUEST_BUCKETS_MS) + 1))
    stream_ms: list[int] = field(default_factory=lambda: [0] * (len(STREAM_BUCKETS_MS) + 1))
    by_class: dict[str, int] = field(default_factory=dict)


class Registry:
    """A ring of one-minute slots. A class rather than module globals so a test
    can hold its own and assert on a distribution it wrote itself — module
    state shared between tests is how a metrics test starts passing because of
    the test that ran before it.

    No lock. Both writers below run in the event loop of a single process, and
    that single-writer assumption is the one thing to revisit if this process
    ever becomes multi-threaded.
    """

    def __init__(self) -> None:
        self._ring = [_Slot() for _ in range(_WINDOWS + 1)]
        # monotonic: a clock change must not print a negative uptime.
        self._started = time.monotonic()

    def _slot(self, minute: int) -> _Slot:
        slot = self._ring[minute % len(self._ring)]
        if slot.minute != minute:
            slot.minute = minute
            slot.request_ms = [0] * (len(REQUEST_BUCKETS_MS) + 1)
            slot.stream_ms = [0] * (len(STREAM_BUCKETS_MS) + 1)
            slot.by_class = {}
        return slot

    def record_response_start(self, path: str, status: int, elapsed_ms: float) -> None:
        if path.startswith(_NOT_MEASURED):
            return
        slot = self._slot(int(time.time()) // _WINDOW_SECONDS)
        # The last index is the overflow: samples above the top edge. Kept and
        # published separately, because folding them into the top bucket lets
        # a nine-second request be reported as 5000 ms.
        slot.request_ms[bisect.bisect_left(REQUEST_BUCKETS_MS, elapsed_ms)] += 1
        klass = f"{status // 100}xx"
        slot.by_class[klass] = slot.by_class.get(klass, 0) + 1

    def record_stream_duration(self, elapsed_ms: float) -> None:
        slot = self._slot(int(time.time()) // _WINDOW_SECONDS)
        slot.stream_ms[bisect.bisect_left(STREAM_BUCKETS_MS, elapsed_ms)] += 1

    def snapshot(self) -> "ServiceStatus":
        """Sum the last `_WINDOWS` minutes, current minute included.

        A slot whose `minute` does not match the minute it is being read for
        is either untouched (`-1`, at process start) or more than `_WINDOWS`
        minutes stale — the ring wrapped past it without a write landing there
        again. Either way it contributes nothing rather than a leftover count
        from a much earlier window.
        """
        current_minute = int(time.time()) // _WINDOW_SECONDS
        request_counts = [0] * (len(REQUEST_BUCKETS_MS) + 1)
        stream_counts = [0] * (len(STREAM_BUCKETS_MS) + 1)
        by_class: dict[str, int] = {}

        for offset in range(_WINDOWS):
            minute = current_minute - offset
            slot = self._ring[minute % len(self._ring)]
            if slot.minute != minute:
                continue
            for index, count in enumerate(slot.request_ms):
                request_counts[index] += count
            for index, count in enumerate(slot.stream_ms):
                stream_counts[index] += count
            for klass, count in slot.by_class.items():
                by_class[klass] = by_class.get(klass, 0) + count

        return ServiceStatus(
            commit=_commit(),
            uptime_seconds=int(time.monotonic() - self._started),
            window_seconds=_WINDOW_SECONDS * _WINDOWS,
            requests=RequestCounts(total=sum(request_counts), by_class=by_class),
            time_to_response_start=_percentiles(REQUEST_BUCKETS_MS, request_counts),
            stream_duration=_percentiles(STREAM_BUCKETS_MS, stream_counts),
        )


_registry = Registry()  # the process-wide default; `snapshot()` reads this one


def record_response_start(path: str, status: int, elapsed_ms: float) -> None:
    _registry.record_response_start(path, status, elapsed_ms)


def record_stream_duration(elapsed_ms: float) -> None:
    _registry.record_stream_duration(elapsed_ms)


def snapshot() -> "ServiceStatus":
    return _registry.snapshot()


class Percentiles(BaseModel):
    # Every field is nullable and `samples` is always present, because a
    # percentile of nothing is not zero. A panel that renders 0 ms for an
    # idle window states a measurement that was never taken.
    p50_ms: int | None
    p95_ms: int | None
    p99_ms: int | None
    samples: int
    # Samples above the top bucket edge. A null percentile with a non-zero
    # `above_count` means "past the top of the ladder", which is a different
    # statement from "unknown" and from the top edge itself.
    above_ms: int
    above_count: int


class RequestCounts(BaseModel):
    total: int
    by_class: dict[str, int]  # "2xx" / "4xx" / "5xx" — never per endpoint


class ServiceStatus(BaseModel):
    commit: str
    uptime_seconds: int
    window_seconds: int
    requests: RequestCounts
    time_to_response_start: Percentiles = Field(
        description=(
            "Time from the start of request handling to the response status line, over "
            "every route except the SSE stream, this endpoint and the health probes."
        )
    )
    stream_duration: Percentiles = Field(
        description="How long SSE streams stayed open. Not a latency: it is mostly the model."
    )


def _percentile(edges: tuple[int, ...], counts: list[int], q: float) -> int | None:
    total = sum(counts)
    if total == 0:
        return None
    target = q * total
    seen = 0
    lower = 0.0
    for index, count in enumerate(counts):
        if count and seen + count >= target:
            if index == len(edges):
                # The overflow bucket. Returning the top edge here would
                # report a 45-second stream as 5000 ms, which is the specific
                # lie this function must not tell; `above_count` says what is
                # known.
                return None
            upper = edges[index]
            return round(lower + (upper - lower) * (target - seen) / count)
        seen += count
        if index < len(edges):
            lower = edges[index]
    return None


def _percentiles(edges: tuple[int, ...], counts: list[int]) -> Percentiles:
    return Percentiles(
        p50_ms=_percentile(edges, counts, 0.50),
        p95_ms=_percentile(edges, counts, 0.95),
        p99_ms=_percentile(edges, counts, 0.99),
        samples=sum(counts),
        above_ms=edges[-1],
        above_count=counts[-1],
    )


_GIT_SHA_SHAPE = re.compile(r"\A[0-9a-f]{7,40}\Z")


def _commit() -> str:
    """What is running, as far as the image can honestly say.

    The shape check is because this value reaches a public response straight
    from the environment. Anything that can set a variable on the container
    could otherwise choose what a visitor reads in a field that looks like a
    fact about the deployment.
    """
    value = os.environ.get("GIT_SHA", "")
    return value if _GIT_SHA_SHAPE.match(value) else "unknown"
