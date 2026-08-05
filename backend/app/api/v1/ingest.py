"""`POST /api/v1/ingest` and `GET /api/v1/ingest/status` — the HTTP surface
over `app.ingest.jobs` (Task 23's admission, staged progress and write).

Two routes, not three. Revision 1 of this task returned `202 {job_id}` from
the POST and sketched a matching `GET /api/v1/ingest/{job_id}`. Nothing
consumes `job_id`: status is keyed on the organisation, because a tenant gets
exactly one ingest for its whole life (Task 23, Step 4) — `GET .../status`
takes no parameter and needs none. An id returned and never used is not
neutral, it is an invitation for the next author to build a third route
around it, and the obvious value for it (the Celery task id) is an internal
Redis key shared with the broker and result backend. So the POST returns
**the same body the GET does** — the initial status snapshot, read back
after `mark_ingest_queued` rather than assembled by hand, so a Redis write
that silently failed is visible here too rather than papered over with an
optimistic literal.

Polling, not SSE. Stream slots (`app/limits.py`) are a bounded resource that
exists for chat answers; an ingest is coarse-grained and finishes in seconds,
and 500 ms polling shows the same picture without spending a slot that a
chat stream might have used instead. `GET .../status` does not hold the
connection open, so it is an ordinary request as far as `app/observability.py`
is concerned — except that its poll rate (twice a second, the most
aggressive in the product) would otherwise dominate the request histogram
and drag its p50 down to describe the poller rather than the service. That
exception (`_NOT_MEASURED` in `app/observability.py`) is owned by Task 25 and
already contains this path; `test_ingest_api.py` checks — rather than
assumes — that a real request against the mounted route is excluded.

Both routes require a bearer token (`CurrentPrincipal`) and act only on
`principal.org_id`, never a caller-supplied one — the same tenant boundary
every other authenticated route on this surface holds. See
`EXPECTED_ROUTES` (`backend/tests/test_demo_orgs.py`) for the residual-purge
argument these two routes make against migration 0004.
"""

from enum import StrEnum

from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from app.auth.deps import CurrentPrincipal
from app.errors import ProblemError
from app.ingest.catalogue import CATALOGUE
from app.ingest.jobs import ensure_ingest_admitted, mark_ingest_queued, read_ingest_status
from app.worker.tasks import ingest_source

router = APIRouter(prefix="/api/v1", tags=["ingest"])


class IngestStage(StrEnum):
    """The stages `app.ingest.jobs.run_ingest` writes, in order, plus the two
    it does not: `idle` (nothing ever queued for this organisation) and
    `error` (the job raised — see `app.worker.tasks.ingest_source`)."""

    IDLE = "idle"
    QUEUED = "queued"
    PARSING = "parsing"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    INDEXING = "indexing"
    COMPLETE = "complete"
    ERROR = "error"


class IngestRequest(BaseModel):
    source_key: str = Field(min_length=1, max_length=100)


class IngestStatusResponse(BaseModel):
    stage: IngestStage
    chunks_done: int
    chunks_total: int
    error: str | None = Field(
        description=(
            "A stable code, never a message and never an exception's text: "
            "`ingest_failed`, `ingest_timeout`, `ingest_abandoned` or "
            "`already_ingested` (app/worker/tasks.py). Null unless `stage` is "
            "`error`. Whatever went wrong is in the worker's log, correlated by "
            "organisation — a database failure's own string carries the failing "
            "statement, its bound parameters and the document text with it, and "
            "this field is returned to the visitor verbatim."
        ),
    )
    started_at: int | None = Field(
        description="Unix seconds. Null until mark_ingest_queued has run for this organisation."
    )


def _source_not_found() -> ProblemError:
    return ProblemError(
        code="source_not_found",
        status=404,
        title="Source not found",
        detail="No such source in the catalogue.",
    )


@router.post("/ingest", status_code=status.HTTP_202_ACCEPTED)
async def start_ingest(
    payload: IngestRequest, principal: CurrentPrincipal
) -> IngestStatusResponse:
    """Admit, queue and dispatch — in that order, and none of it skipped on
    a race. `ensure_ingest_admitted` raises `ProblemError` for both ceilings
    (409 already_ingested, 503 capacity_unavailable with Retry-After); a
    catalogue miss is checked first, cheaply, before either touches
    Postgres.
    """
    if payload.source_key not in CATALOGUE:
        raise _source_not_found()

    await ensure_ingest_admitted(principal.org_id)
    await mark_ingest_queued(principal.org_id)
    ingest_source.delay(principal.org_id, payload.source_key)

    return IngestStatusResponse(**await read_ingest_status(principal.org_id))


@router.get("/ingest/status")
async def get_ingest_status(principal: CurrentPrincipal) -> IngestStatusResponse:
    return IngestStatusResponse(**await read_ingest_status(principal.org_id))
