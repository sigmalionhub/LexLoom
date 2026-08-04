"""The corpus the metrics are measured on, and the guard that keeps it honest.

Three candidates were considered for what to evaluate against, and the choice
is the reason this module exists rather than a fixture file:

**(a) The whole M2.2 pipeline in CI** — Redis, a Celery worker, an HTTP ingest
request, waiting for the job to reach `complete`. That reproduces half the stack
in order to take a measurement that says nothing about background processing,
makes the gate's wall time depend on a queue, and couples it to the longest
branch in the plan.

**(b) A committed fixture of a few hundred chunks.** Cheap, and it drifts:
change the chunker and the fixture stays as it was, the metrics stay green, and
production has different chunks from the ones anybody measured.

**(c) Chosen: the same catalogue PDF, through the same parser, in process.**
`CATALOGUE["gdpr-full"]` and `app.ingest.parse` directly, written under
`tenant_session(EVAL_ORG_ID)`, embedded in batches of `EMBED_BATCH` through the
same `Embedder`. No Celery, no Redis, no HTTP. The chunks are byte for byte the
ones an ingest job writes — same source, same chunker — so there is no
translator between what the evals measure and what a tenant holds; and the
drift that (b) hides is caught by `assert_corpus_matches_manifest` below.

The eval tenant's corpus is **the thirty seeded excerpts plus the full
catalogue GDPR**, in the order a live tenant receives them: the seed at
provisioning, the catalogue at ingest. Measuring the catalogue alone would
measure a corpus nobody has. It also matters for scoring: the seeded excerpts
are extracts of the same regulation, so a correct answer can legitimately arrive
as the seeded duplicate, and it has to be nameable when it does.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

# `_INSERT_CHUNK` is imported rather than re-declared, and the leading
# underscore is not an oversight. `chat/corpus.py` says why in its own comment:
# "A second copy of this INSERT is how one of them ends up writing NULL into a
# column the other populates, with nothing raised anywhere." The eval corpus has
# exactly that failure available to it — a column list of its own that forgets
# `source_key` would give every eval chunk a NULL key, and the gold set would
# then score a corpus it cannot name. Reaching for the private name is the
# cheaper mistake to live with than owning a fourth spelling of this statement.
from app.chat.corpus import (
    _INSERT_CHUNK,
    SEED_SOURCE_KEY,
    load_excerpts,
    load_seed_vectors,
)
from app.chat.embedding import get_embedder
from app.config import PUBLIC_ENVS, Settings
from app.evals.score import ChunkKey
from app.ingest.catalogue import CATALOGUE
from app.ingest.parse import parse_source
from app.worker.tasks import EMBED_BATCH

# A fixed id, not a random one, and `expires_at` an hour out rather than NULL.
#
# Fixed: `--write` and `--check` both start by deleting this organisation's
# chunks and rewriting them, so a run that died halfway leaves nothing behind to
# skew the next one. A random id would leave one abandoned ~1057-chunk corpus
# per crashed run.
#
# Expiring: unlike `PUBLIC_CORPUS_ORG_ID`, which is permanent because the
# application needs it, this organisation is scaffolding. ~1057 chunks plus
# their 384-float embeddings is a couple of megabytes, and a permanent copy of
# it would sit in a Postgres container limited to 384 MB forever, for numbers
# nobody reads out of the database. An hour is longer than any run and shorter
# than the gap between them, so `purge_expired_orgs` collects it on its own.
EVAL_ORG_ID = "00000000-0000-0000-0000-000000000002"

EVAL_ORG_TTL = timedelta(hours=1)

# The one catalogue source the gold set is written against. Named here rather
# than iterated over `CATALOGUE`, because adding a second source to the
# catalogue must not silently change what the metrics are measured on.
CATALOGUE_SOURCE_KEY = "gdpr-full"

_HERE = Path(__file__).resolve().parent
MANIFEST_FILE = _HERE / "manifest.json"


class CorpusDriftError(RuntimeError):
    """The corpus is no longer the one the gold set was written against."""


@dataclass(frozen=True)
class SeededChunk:
    """One chunk of the eval corpus, named the way the gold set names it."""

    source_key: str
    chunk_ordinal: int
    content: str
    page_from: int
    page_to: int
    # The row's UUID, known only after `seed_eval_corpus` has generated it.
    # Carried here so the runner can map a `Citation` — which reports
    # `chunk_id` and nothing else identifying — back to a `ChunkKey`, without a
    # second query and without the runner having to trust that ordinals and ids
    # line up.
    chunk_id: uuid.UUID | None = None

    @property
    def key(self) -> ChunkKey:
        return f"{self.source_key}:{self.chunk_ordinal}"


def parse_chunk_key(key: ChunkKey) -> tuple[str, int]:
    """Split `"seed:gdpr-excerpts:7"` into `("seed:gdpr-excerpts", 7)`.

    `rsplit`, never `split`. `SEED_SOURCE_KEY` contains a colon of its own, so
    splitting from the left names the source `"seed"` and the ordinal
    `"gdpr-excerpts"`, which fails as a `ValueError` on the int cast — on a good
    day. On a bad one someone "fixes" it by taking element `[0]` and every
    seeded judgement silently addresses a source that does not exist.
    """
    source_key, _, ordinal = key.rpartition(":")
    if not source_key:
        raise ValueError(f"{key!r} is not a chunk key: expected 'source_key:ordinal'")
    return source_key, int(ordinal)


def content_digest(content: str) -> str:
    """The full sha256 of a chunk's text. The manifest stores the first 16 hex."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _refuse_on_a_public_box(settings: Settings) -> None:
    """Neither `--write` nor `--check` may ever run against the demo deployment.

    Seeding the eval corpus is ~1057 inserts and ~1027 ONNX inferences. The box
    has two cores, one of them already someone else's production, and the API
    runs `--workers 1`. Running this there would stall every open SSE stream for
    the duration of a measurement whose whole purpose is to be reproducible
    somewhere quiet.
    """
    if settings.env in PUBLIC_ENVS:
        raise SystemExit(
            f"refusing to seed an eval corpus with ENV={settings.env}: "
            "evals run locally and in CI, never on a public deployment"
        )


@lru_cache(maxsize=1)
def _catalogue_chunks() -> tuple[SeededChunk, ...]:
    """Parse the catalogue PDF once per process; it costs seconds, not milliseconds."""
    source = CATALOGUE[CATALOGUE_SOURCE_KEY]
    return tuple(
        SeededChunk(
            source_key=source.key,
            chunk_ordinal=chunk.chunk_ordinal,
            content=chunk.content,
            page_from=chunk.page_from,
            page_to=chunk.page_to,
        )
        for chunk in parse_source(source)
    )


@lru_cache(maxsize=1)
def _seed_chunks() -> tuple[SeededChunk, ...]:
    """The thirty excerpts, keyed exactly the way `copy_corpus_into` keys them:
    `SEED_SOURCE_KEY` and the excerpt's index in `load_excerpts()`."""
    return tuple(
        SeededChunk(
            source_key=SEED_SOURCE_KEY,
            chunk_ordinal=ordinal,
            content=excerpt.content,
            page_from=excerpt.page_from,
            page_to=excerpt.page_to,
        )
        for ordinal, excerpt in enumerate(load_excerpts())
    )


def build_corpus() -> list[SeededChunk]:
    """The eval corpus as data, with no database and no model involved.

    Seed first, catalogue second — the order a live tenant accumulates them in.
    The order is not merely cosmetic: `corpus_fingerprint` hashes a sorted view
    precisely so that it is *not* sensitive to this, while the manifest's
    per-source counts are.
    """
    return [*_seed_chunks(), *_catalogue_chunks()]


def corpus_fingerprint(chunks: Sequence[SeededChunk]) -> str:
    """sha256 over the sorted `(source_key, chunk_ordinal, sha256(content))` triples.

    Deliberately not a hash of the PDF and not a chunk count. What the gold set
    depends on is the mapping from ordinals to text, and that is what this
    hashes: a chunker change, a source change and a re-ordering all move it,
    while a re-run on the same inputs on a different machine does not.

    Float embeddings are excluded on purpose — ONNX output differs in the last
    bits across CPUs, and a fingerprint that changes when the runner changes is
    a fingerprint people learn to regenerate without reading.
    """
    triples = sorted(
        (chunk.source_key, chunk.chunk_ordinal, content_digest(chunk.content))
        for chunk in chunks
    )
    payload = json.dumps(triples, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --- the manifest -------------------------------------------------------------


@dataclass(frozen=True)
class Manifest:
    sources: Mapping[str, int]
    chunks: Mapping[ChunkKey, str]

    @property
    def total(self) -> int:
        return sum(self.sources.values())


def build_manifest(chunks: Sequence[SeededChunk], referenced: Sequence[ChunkKey]) -> Manifest:
    """Count every source; pin the content of the chunks the gold set names.

    Only the referenced ordinals get a hash — sixty-odd of a thousand — which
    keeps the artefact reviewable in a diff while still covering every chunk a
    metric can depend on. The per-source counts catch a renumbering that happens
    to leave the referenced ordinals intact.
    """
    sources: dict[str, int] = {}
    for chunk in chunks:
        sources[chunk.source_key] = sources.get(chunk.source_key, 0) + 1
    by_key = {chunk.key: chunk for chunk in chunks}
    pinned: dict[ChunkKey, str] = {}
    for key in sorted(set(referenced), key=parse_chunk_key):
        if key not in by_key:
            raise CorpusDriftError(
                f"the gold set names {key}, which the corpus does not contain — "
                "the manifest cannot be written for a judgement about a missing chunk"
            )
        pinned[key] = content_digest(by_key[key].content)[:16]
    return Manifest(sources=dict(sorted(sources.items())), chunks=pinned)


def write_manifest(manifest: Manifest, path: Path = MANIFEST_FILE) -> None:
    path.write_text(
        json.dumps(
            {"sources": dict(manifest.sources), "chunks": dict(manifest.chunks)},
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def load_manifest(path: Path = MANIFEST_FILE) -> Manifest:
    document = json.loads(path.read_text(encoding="utf-8"))
    return Manifest(sources=document["sources"], chunks=document["chunks"])


def assert_corpus_matches_manifest(
    chunks: Sequence[SeededChunk], manifest: Manifest
) -> None:
    """Fail here, loudly, rather than three metrics later, quietly.

    The gold set names chunks by position: `(source_key, chunk_ordinal)`. A
    change to the chunker's window, its overlap or its paragraph rule renumbers
    the corpus without touching a single line of the gold set, and every metric
    then scores the wrong chunks and reports a number that looks exactly like a
    retrieval regression. This is the one failure mode of this phase that
    produces a confident lie rather than an error.

    Called before the first search, in `--write` and in `--check` alike. Drift
    has to stop a measurement, not enter one.

    The per-source counts are checked before the hashes because they localise
    the problem better: "gdpr-full has 1041 chunks, the manifest was written for
    1027" names the source that moved, where a hash mismatch on ordinal 184 only
    says something moved somewhere before it.
    """
    counts: dict[str, int] = {}
    for chunk in chunks:
        counts[chunk.source_key] = counts.get(chunk.source_key, 0) + 1
    if counts != dict(manifest.sources):
        raise CorpusDriftError(
            f"the corpus has {counts}, the manifest was written for "
            f"{dict(manifest.sources)}: the chunker or a source file changed, and every "
            "chunk_ordinal in the gold set may now point at different text"
        )
    if len(chunks) != manifest.total:
        raise CorpusDriftError(
            f"the corpus has {len(chunks)} chunks, the manifest was written for "
            f"{manifest.total}"
        )

    by_key = {chunk.key: chunk for chunk in chunks}
    for key, expected in manifest.chunks.items():
        chunk = by_key.get(key)
        if chunk is None:
            raise CorpusDriftError(
                f"{key}: the manifest pins this ordinal and the corpus no longer has it"
            )
        actual = content_digest(chunk.content)[:16]
        if actual != expected:
            raise CorpusDriftError(
                f"{key}: manifest {expected}, corpus {actual}. The gold set says this "
                "ordinal answers a question; it no longer holds the text it was "
                "written against. Re-derive the gold set, then --write-manifest."
            )


# --- seeding ------------------------------------------------------------------


_ENSURE_ORG = text(
    "INSERT INTO organizations (id, external_id, name, expires_at) "
    "VALUES (:id, 'evals', 'Retrieval evaluation', :expires_at)"
)

_TOUCH_ORG = text("UPDATE organizations SET expires_at = :expires_at WHERE id = :id")

_DELETE_CHUNKS = text("DELETE FROM document_chunks WHERE org_id = :org_id")

_COUNT_UNNAMED = text(
    "SELECT count(*) FROM document_chunks WHERE org_id = :org_id AND source_key IS NULL"
)


def embed_corpus(chunks: Sequence[SeededChunk]) -> list[list[float]]:
    """One vector per chunk, in the same order.

    The seeded excerpts take their vectors from `gdpr_excerpts_vectors.json`
    rather than the model — the same file every demo tenant's copy is written
    from — so the eval corpus and a live tenant's corpus hold the *identical*
    vector for the same excerpt, byte for byte, and a difference in the metrics
    can never be a difference in which machine embedded the seed.

    Catalogue chunks have no precomputed file and are embedded here, in batches
    of `EMBED_BATCH`. That constant is imported rather than written out: 32 is a
    ratified memory ceiling (253 MiB at 16, 315 at 32, 444 at 64, against a
    worker limited to 650m), and a second spelling of it here is a second place
    somebody can raise it without re-measuring.
    """
    seed_vectors = {chunk.key: list(vector) for chunk, vector in
                    zip(_seed_chunks(), load_seed_vectors(), strict=True)}
    vectors: list[list[float] | None] = [seed_vectors.get(chunk.key) for chunk in chunks]

    pending = [index for index, vector in enumerate(vectors) if vector is None]
    if pending:
        embedder = get_embedder()
        for start in range(0, len(pending), EMBED_BATCH):
            batch = pending[start : start + EMBED_BATCH]
            encoded = embedder.encode([chunks[index].content for index in batch])
            for index, vector in zip(batch, encoded, strict=True):
                vectors[index] = vector
    return [vector for vector in vectors if vector is not None]


async def seed_eval_corpus(session: AsyncSession) -> list[SeededChunk]:
    """Rewrite the eval organisation's corpus and return it, ids and all.

    Idempotent by replacement, like `seed_public_corpus`: the chunks are deleted
    and rewritten, so a crashed run leaves nothing for the next one to measure.

    The session must already be scoped to `EVAL_ORG_ID` — `document_chunks`
    carries a WITH CHECK on every insert, so a row stamped with another
    organisation is refused by Postgres rather than written.

    Embedding happens in `embed_corpus`, **before** this is called, and
    deliberately outside the transaction: ~1027 ONNX inferences is over a minute
    of CPU, and holding a transaction open across it turns a measurement into
    lock contention with anything else using the same database.
    """
    now = datetime.now(UTC)
    expires_at = now + EVAL_ORG_TTL
    try:
        async with session.begin_nested():
            await session.execute(_ENSURE_ORG, {"id": EVAL_ORG_ID, "expires_at": expires_at})
    except IntegrityError:
        await session.execute(_TOUCH_ORG, {"id": EVAL_ORG_ID, "expires_at": expires_at})

    await session.execute(_DELETE_CHUNKS, {"org_id": EVAL_ORG_ID})

    chunks = build_corpus()
    vectors = embed_corpus(chunks)
    seeded = [
        SeededChunk(
            source_key=chunk.source_key,
            chunk_ordinal=chunk.chunk_ordinal,
            content=chunk.content,
            page_from=chunk.page_from,
            page_to=chunk.page_to,
            chunk_id=uuid.uuid4(),
        )
        for chunk in chunks
    ]
    await session.execute(
        _INSERT_CHUNK,
        [
            {
                "id": str(chunk.chunk_id),
                "org_id": EVAL_ORG_ID,
                "content": chunk.content,
                "page_from": chunk.page_from,
                "page_to": chunk.page_to,
                "source_key": chunk.source_key,
                "chunk_ordinal": chunk.chunk_ordinal,
                "embedding": vector,
            }
            for chunk, vector in zip(seeded, vectors, strict=True)
        ],
    )
    return seeded


async def count_unnamed_chunks(session: AsyncSession) -> int:
    """How many of the eval tenant's chunks carry no `(source_key, chunk_ordinal)`.

    A NULL `source_key` is a chunk the gold set cannot name, and it can still be
    returned by a search. The seeded excerpts are extracts of the same GDPR text
    the catalogue source holds, so a correct answer can arrive as the seeded
    duplicate; scored against a gold set keyed on `(source_key, chunk_ordinal)`,
    an unaddressable duplicate counts as a miss — the metric drops and retrieval
    had nothing to do with it.
    """
    return int(await session.scalar(_COUNT_UNNAMED, {"org_id": EVAL_ORG_ID}) or 0)
