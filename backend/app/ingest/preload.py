"""The catalogue corpus, loaded once into the public template organisation.

Every demo tenant is born holding the whole GDPR. It does not get there by
running an ingest per visitor — that is a minute of CPU and 978 ONNX inferences
against a shared box, on the path that has to paint first. It gets there by
being copied, inside Postgres, from one organisation that holds it already.

This module fills that organisation. It runs at deployment, not at request time,
and `deploy/README.md` names it as a step **before** the API starts serving:
a box where it has not run hands every visitor a tenant with the thirty seeded
excerpts and no document, and `provision_demo_org` is what refuses to let that
pass quietly.

Why a third writer of `document_chunks` rather than a fourth caller of an
existing one: `app/chat/corpus.py` owns the seed file's INSERT and says so;
`app/ingest/jobs.py` owns the per-tenant ingest's and says why it is separate;
`app/evals/corpus.py` writes the eval organisation's. This one writes
organisation-independent template rows from the catalogue, which is none of
those three. It reuses their statement where the columns are identical — the
statement, not the semantics.
"""

from __future__ import annotations

import uuid

import structlog
from pgvector.sqlalchemy import Vector
from sqlalchemy import bindparam, text

from app.chat.corpus import PUBLIC_CORPUS_ORG_ID, load_excerpts
from app.chat.embedding import DIMENSIONS, get_embedder
from app.db.session import tenant_session
from app.ingest.catalogue import CATALOGUE, Source
from app.ingest.parse import parse_source
from app.worker.tasks import EMBED_BATCH

logger = structlog.get_logger(__name__)

# Nine columns, and its own statement rather than `app/chat/corpus.py`'s.
#
# That one writes eight — it has no `locator`, because the seed excerpts are
# pages of a PDF and have none — and its comment scopes its single-owner rule to
# "the seed corpus's two callers" for exactly this reason. Importing it here
# would work today, because the GDPR is a PDF too and its locator is NULL all
# the way down; it would stop working silently the day a `.docx` joined
# `PRELOADED_SOURCE_KEYS`, writing NULL into the only position that document's
# citations have. `app/ingest/jobs.py` keeps a third copy on the same reasoning.
#
# `bindparam(..., type_=Vector(DIMENSIONS))` because `register_vector` is called
# nowhere in this repository: asyncpg holds no codec for `vector` and rejects a
# bare `list[float]` outright.
_INSERT_CHUNK = text(
    "INSERT INTO document_chunks "
    "(id, org_id, content, page_from, page_to, locator, source_key, chunk_ordinal, embedding) "
    "VALUES (:id, :org_id, :content, :page_from, :page_to, :locator, :source_key, "
    ":chunk_ordinal, :embedding)"
).bindparams(bindparam("embedding", type_=Vector(DIMENSIONS)))

# The one source every tenant is given at provisioning. A tuple rather than a
# bare string because the day a second document joins it, the list of things
# `copy_catalogue_corpus_into` copies and the list migration 0012's predicate
# excludes have to move together — and a tuple makes that a visible edit rather
# than a string that quietly gains a comma somewhere else.
PRELOADED_SOURCE_KEYS: tuple[str, ...] = ("gdpr-full",)

_DELETE_SOURCE = text(
    "DELETE FROM document_chunks WHERE org_id = :org_id AND source_key = :source_key"
)


async def seed_public_catalogue_corpus() -> dict[str, int]:
    """Write every preloaded catalogue source into the public organisation.

    Returns `{source_key: rows}`. Idempotent by replacement, like
    `seed_public_corpus`: the rows for that source are deleted and rewritten, so
    running it on every deployment leaves one copy rather than one more.

    Deliberately narrower than "delete everything for this organisation": the
    seeded excerpts live in the same organisation and are written by a different
    loader. A blanket DELETE here would make the two loaders order-dependent,
    and the order that broke it would be the one somebody ran by hand.
    """
    written: dict[str, int] = {}
    for key in PRELOADED_SOURCE_KEYS:
        written[key] = await _load_one(CATALOGUE[key])
    return written


async def _load_one(source: Source) -> int:
    """Parse, embed, then write — and embed **outside** the transaction.

    978 inferences is roughly a minute of CPU. Holding a write transaction open
    across it turns a deployment step into lock contention with whatever else is
    using the database, which is the same reason `app/evals/corpus.py` embeds
    before it opens its session.
    """
    chunks = parse_source(source)
    if source.expected_chunks is not None and len(chunks) != source.expected_chunks:
        # The catalogue's digest already pins the file; this pins the parse. A
        # template corpus that silently held a different number of chunks than
        # the one the gold set was bound to would move every eval metric without
        # failing anything.
        raise RuntimeError(
            f"{source.key}: parsed {len(chunks)} chunks, catalogue expects "
            f"{source.expected_chunks}"
        )

    embedder = get_embedder()
    vectors: list[list[float]] = []
    for start in range(0, len(chunks), EMBED_BATCH):
        batch = chunks[start : start + EMBED_BATCH]
        vectors.extend(embedder.encode([chunk.content for chunk in batch]))

    org_id = str(PUBLIC_CORPUS_ORG_ID)
    async with tenant_session(org_id) as session:
        await session.execute(_DELETE_SOURCE, {"org_id": org_id, "source_key": source.key})
        await session.execute(
            _INSERT_CHUNK,
            [
                {
                    "id": str(uuid.uuid4()),
                    "org_id": org_id,
                    "content": chunk.content,
                    "page_from": chunk.page_from,
                    "page_to": chunk.page_to,
                    # One of the two is always set and the other always NULL: a
                    # paginated source says where a chunk is with pages, a
                    # `.docx` says it with its heading path.
                    "locator": chunk.locator,
                    "source_key": source.key,
                    "chunk_ordinal": chunk.chunk_ordinal,
                    "embedding": vector,
                }
                for chunk, vector in zip(chunks, vectors, strict=True)
            ],
        )

    logger.info("public_catalogue_corpus_seeded", source_key=source.key, chunks=len(chunks))
    return len(chunks)

def provisioned_corpus_size() -> int:
    """How many chunks a freshly provisioned tenant holds.

    One expression, because four tests and a runbook otherwise each carry their
    own arithmetic over the same two facts. `expected_chunks` is measured
    against the committed file rather than guessed, so this number moves only
    when the corpus really does.
    """
    preloaded = sum(CATALOGUE[key].expected_chunks or 0 for key in PRELOADED_SOURCE_KEYS)
    return len(load_excerpts()) + preloaded
