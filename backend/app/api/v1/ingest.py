"""`POST /api/v1/ingest` and `GET /api/v1/ingest/status` — the HTTP surface
over `app.ingest.jobs` (Task 23's admission, staged progress and write).

Two routes, not three. Revision 1 of this task returned `202 {job_id}` from
the POST and sketched a matching `GET /api/v1/ingest/{job_id}`. Nothing
consumes `job_id`, and that is still true: status is keyed on the pair
(organisation, source), and `GET .../status` returns every catalogue source at
once rather than taking a parameter, so there is nothing an id would address.

What did change is the "one ingest for a tenant's whole life" rule this
docstring used to rest on. A tenant may now hold every document in the
catalogue, one ingest per (organisation, source); the duplicate guard, the
progress key and the rows fallback are all keyed on that pair. The global
`INGEST_ORG_CAP` still counts *tenants*, not documents — see migration 0010,
which had to stop counting an ingesting tenant twice once both could be true of
it at once.

An id returned and never used is not neutral: it is an invitation for the next
author to build a third route around it, and the obvious value for it (the
Celery task id) is an internal Redis key shared with the broker and result
backend. So the POST returns
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
from app.ingest.preload import PRELOADED_SOURCE_KEYS
from app.worker.tasks import ingest_source

router = APIRouter(prefix="/api/v1", tags=["ingest"])


class IngestStage(StrEnum):
    """The stages `app.ingest.jobs.run_ingest` writes, in order, plus the two
    it does not: `idle` (nothing ever queued for this organisation *and this
    source* — the pair, since a tenant may hold several documents) and `error`
    (the job raised — see `app.worker.tasks.ingest_source`)."""

    IDLE = "idle"  # nothing ever queued for this organisation AND this source
    QUEUED = "queued"
    PARSING = "parsing"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    INDEXING = "indexing"
    COMPLETE = "complete"
    ERROR = "error"


class IngestRequest(BaseModel):
    source_key: str = Field(min_length=1, max_length=100)


class CatalogueEntry(BaseModel):
    """What a caller may know about a source: seven fields, and no more.

    `Source` (`app/ingest/catalogue.py`) also carries `path`, `sha256`,
    `document_xml_sha256` and `conversion`. None of them leave the server.
    `path` is an absolute path inside the image, and `conversion` names the
    build tool and its exact version — neither is any of a visitor's business,
    and a response model that listed the dataclass's fields by default would
    have shipped both. The model is the boundary, not a convenience.

    `download` is `path.name` and not `path`: the file name is what the export
    serves the document under, while the directory it sits in inside the image
    is exactly the part that stays here.
    """

    key: str
    title: str
    media_type: str
    download: str = Field(
        description=(
            "The file name this document is served under, below `/sources/`. "
            "Served by nginx out of the static export, never by this API: the "
            "API runs one worker and concurrent SSE streams are the resource "
            "`app/limits.py` exists to protect, so a megabyte of PDF must not "
            "go through it. The name comes from the catalogue rather than being "
            "assembled in the page, so the copy step and the link cannot drift."
        ),
    )
    source_url: str = Field(
        description=(
            "Where the publisher puts it. Present because redistribution "
            "requires it, not as a convenience: EUR-Lex permits reuse *with "
            "acknowledgement of source* (Commission Decision 2011/833/EU) and "
            "NIST requests a credit, so a page that offers the file for download "
            "and names no source would breach the terms the catalogue quotes. It "
            "is also the better evidence — a file this demo hands out proves "
            "only that the demo is consistent with itself."
        ),
    )
    preloaded: bool = Field(
        description=(
            "True when every tenant is given this document at sign-in rather "
            "than ingesting it. The page draws the two differently and says "
            "which happened, and it cannot infer this: a preloaded document and "
            "one ingested an hour ago both read `complete`. Derived from "
            "`PRELOADED_SOURCE_KEYS`, so the frontend never spells a source key."
        ),
    )
    suggestions: list[str] = Field(
        description=(
            "Questions the page offers once a tenant holds this document. May "
            "be empty. They travel with the catalogue rather than living in the "
            "frontend for the same reason this route exists: a question is a "
            "property of the document, and a copy in the page would drift from "
            "the corpus that has to answer it."
        ),
    )


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


@router.get("/catalogue")
async def get_catalogue() -> list[CatalogueEntry]:
    """The documents this deployment can ingest.

    This route did not exist while the catalogue held one document, and the
    module docstring above used to argue against adding one: a route built for
    a single caller and a single value is the same mistake as a `job_id` nobody
    reads. With a second source the panel has to render a list it cannot know in
    advance, and hard-coding the pair in the frontend would put the catalogue in
    two places that drift.

    Unauthenticated, for the reason `GET /api/v1/generators` gives for itself:
    it takes no token, sees no organisation and reads no org-scoped table, so it
    carries none of the residual-purge argument migration 0004 makes for a
    credential. What it exposes is what a `/docs` reader already sees in the
    schema — which documents exist — and not one field about where they live on
    disk or how they were built.

    Ordered as `CATALOGUE` is ordered, which is insertion order and therefore
    stable across runs: the panel renders the list as it arrives, and a set-like
    iteration would reshuffle the visitor's rows between polls.
    """
    return [
        CatalogueEntry(
            key=source.key,
            title=source.title,
            media_type=source.media_type,
            download=source.path.name,
            source_url=source.source_url,
            preloaded=source.key in PRELOADED_SOURCE_KEYS,
            suggestions=list(source.suggestions),
        )
        for source in CATALOGUE.values()
    ]


@router.post("/ingest", status_code=status.HTTP_202_ACCEPTED)
async def start_ingest(payload: IngestRequest, principal: CurrentPrincipal) -> IngestStatusResponse:
    """Admit, queue and dispatch — in that order, and none of it skipped on
    a race. `ensure_ingest_admitted` raises `ProblemError` for both ceilings
    (409 already_ingested, 503 capacity_unavailable with Retry-After); a
    catalogue miss is checked first, cheaply, before either touches
    Postgres.
    """
    if payload.source_key not in CATALOGUE:
        raise _source_not_found()

    await ensure_ingest_admitted(principal.org_id, payload.source_key)
    await mark_ingest_queued(principal.org_id, payload.source_key)
    ingest_source.delay(principal.org_id, payload.source_key)

    return IngestStatusResponse(**await read_ingest_status(principal.org_id, payload.source_key))


@router.get("/ingest/status")
async def get_ingest_status(principal: CurrentPrincipal) -> dict[str, IngestStatusResponse]:
    """Every catalogue source's status for this organisation, keyed by source.

    A map rather than one status, and rather than a `?source_key=` parameter,
    because the panel polls this twice a second while anything is in flight: one
    request describing the whole catalogue costs what one source used to, where
    a parameterised route would cost one request per document. The exclusion in
    `app/observability.py`'s `_NOT_MEASURED` was granted on that poll rate and
    still holds for the same reason.

    Every key in the catalogue appears, whether or not this tenant has touched
    it — a source the tenant has never ingested reports `idle`. A caller that
    had to distinguish "absent from the map" from "not started" would be
    deciding something the server already knows.
    """
    return {
        key: IngestStatusResponse(**await read_ingest_status(principal.org_id, key))
        for key in CATALOGUE
    }
