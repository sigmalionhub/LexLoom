"""`GET /api/v1/evals` — the committed retrieval measurement, served as a file.

This route hands back `app/evals/results.json` verbatim. It does not measure
anything, and the fact that it does not is the whole design.

**Why not recompute.** Thirty questions times three modes is ninety searches,
and thirty of them need an ONNX inference for the query vector. The deployment
box has two cores, one of which belongs to somebody else's production, and the
API runs `--workers 1`. An endpoint that recomputed on demand would be a free
denial-of-service lever that needs no authentication to pull.

**And the cost is not only CPU.** Recomputing means seeding ~1008 chunks for an
organisation *over HTTP* — child rows created at an arbitrary moment, including
the moment after `purge_expired_orgs` has taken its second snapshot. That is
precisely the residual window migration `0004` documents at lines 63-72. Serving
a file is what keeps the answer written beside this route's entry in
`EXPECTED_ROUTES` (`backend/tests/test_demo_orgs.py`) true: no credential minted,
no `org_id` accepted, no `tenant_session` opened, and no SQL executed at all.
`test_evals_endpoint.py` holds that last claim by counting statements on the
engine rather than by trusting this paragraph.

**Unauthenticated on purpose**, for the same reason `GET /api/v1/status` and
`GET /api/v1/generators` are: there is nothing about a tenant in the response.
These are numbers about our own public catalogue, of the same class as the
README. Because the answer is a constant in process memory it also needs no slot
from `app/limits.py` and no `limit_req` zone in nginx.

**A missing file is `503`, never `200` with zeros.** The panel renders a table,
and a table of zeros reads as "retrieval is broken" rather than as "this build
did not carry the artefact" — which is the true statement, and the actionable
one.
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

import app.evals
from app.errors import ProblemError

router = APIRouter(prefix="/api/v1", tags=["evals"])

# Located through the package rather than by counting `..` from this file, so
# moving either module leaves this pointing at the artefact instead of at
# nothing. `app.evals.__init__` imports nothing, so naming it here does not drag
# the runner — and through it SQLAlchemy, the retrieval module and fastembed —
# into the API process.
RESULTS_FILE = Path(app.evals.__file__).resolve().parent / "results.json"


class EvalsCorpus(BaseModel):
    """What was searched. `sources` maps a catalogue key to its chunk count."""

    sources: dict[str, int]
    chunks: int


class EvalsQuestions(BaseModel):
    """Thirty questions, six of which have no answer in the corpus *by design*.

    The split is in the payload rather than left to the reader because without
    it `precision_at_3 = 0.18` invites the reading "82% broken", when a fifth of
    the set is asking for a miss.
    """

    total: int
    answerable: int
    unanswerable: int


class EvalsProvenance(BaseModel):
    """What the numbers are about — the fields the CI freshness check compares.

    `goldset_sha256` and `corpus_fingerprint` are hashes of the questions and of
    the parsed corpus, so a build whose gold set or catalogue has moved on cannot
    quietly keep publishing the old measurement under a fresh-looking date.
    """

    goldset_sha256: str
    corpus_fingerprint: str
    embedding_model: str
    limit: int
    modes: list[str]


class EvalsModeMetrics(BaseModel):
    """One mode's five numbers, plus the same four again split by question kind.

    Every metric is optional because `None` is a real answer and zero is not:
    recall, reciprocal rank and nDCG are undefined on a question with no relevant
    chunk, and `answerability_auc` is undefined when every top-1 score is equal.
    Reporting 0.0 there would be a claim of failure where there is no
    measurement.
    """

    precision_at_3: float | None = None
    recall_at_5: float | None = None
    mrr_at_10: float | None = None
    ndcg_at_10: float | None = None
    answerability_auc: float | None = None
    by_kind: dict[str, dict[str, float | None]] = Field(default_factory=dict)


class EvalsResponse(BaseModel):
    """The stored artefact. `measured_at` and `commit` say when and on what.

    Both matter more than usual here: this is the one endpoint whose body was
    computed long before the request, so a reader who cannot see its age cannot
    tell a current measurement from a stale one.
    """

    schema_version: int = Field(alias="schema")
    measured_at: str
    commit: str
    provenance: EvalsProvenance
    corpus: EvalsCorpus
    questions: EvalsQuestions
    modes: dict[str, EvalsModeMetrics]

    model_config = {"populate_by_name": True}


@lru_cache(maxsize=1)
def _load_results() -> dict[str, Any] | None:
    """Read the artefact once per process, never per request.

    Cached rather than read each time because the file cannot change under a
    running process: it is baked into the image, and a build that replaces it
    replaces the container. `None` rather than an exception so a missing file
    stays a 503 on every request instead of a 503 followed by a cache that
    remembers nothing.
    """
    try:
        return json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


@router.get("/evals", response_model=EvalsResponse, response_model_by_alias=True)
async def read_evals() -> EvalsResponse:
    document = _load_results()
    if document is None:
        raise ProblemError(
            code="evals_unavailable",
            status=503,
            title="No retrieval measurement in this build",
            detail=(
                "app/evals/results.json is missing from this build, so there are no "
                "retrieval numbers to report. Run `python -m app.evals.run --write` and "
                "commit the result."
            ),
        )
    return EvalsResponse.model_validate(document)
