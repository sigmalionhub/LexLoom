"""The public seed corpus: open legal text every tenant starts out owning.

The demo lets an anonymous visitor ask a legal question and see the sources
before the first token arrives. That needs a corpus, and a visitor arrives with
nothing — so one is copied into their organisation when it is created.

Copied, not shared. The alternative is one set of rows and a policy exception
letting every tenant read them, which would be cheaper and would put a
permanent hole in the one property this repository exists to demonstrate, in
the one table a visitor actually reads. A copy costs a few dozen rows per
visitor and the 24-hour purge takes them away again.
"""

import hashlib
import json
import pathlib
import uuid
from dataclasses import dataclass
from functools import lru_cache

from pgvector.sqlalchemy import Vector
from sqlalchemy import bindparam, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.embedding import DIMENSIONS, MODEL_NAME
from app.db.session import tenant_session

# The organisation `scripts/seed_corpus.py` loads the corpus into: permanent
# (`expires_at IS NULL`, so the purge never reaches it) and connected to nobody.
# It is not what demo tenants read — they read their own copy — so it holds no
# privilege and needs no policy exception. It exists so the corpus is present in
# the database as data rather than only as a file, which is what the M2 embedding
# work will need.
PUBLIC_CORPUS_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

# Outside `app/` because it is source material rather than code, and next to it
# in every environment: the image copies the whole `backend/` tree to /app, and
# development imports `app` from the working tree. A non-editable
# `pip install .` would put `app` in site-packages and leave this behind, which
# is why the loader below fails loudly with the path it looked at rather than
# returning an empty corpus.
SEED_FILE = pathlib.Path(__file__).resolve().parents[2] / "seed" / "gdpr_excerpts.json"

# The excerpts' embeddings, computed once by `scripts/embed_seed_corpus.py` and
# committed next to the text they describe. Next to it and not derived at
# runtime, because the alternative is thirty ONNX inferences on the demo-login
# path: `copy_corpus_into` is called from `provision_demo_org` inside an open
# transaction, on an API running `--workers 1`, so every concurrent SSE stream
# would pause for the 0.6-2 s that costs on a shared vCPU.
#
# A second benefit that only shows up later: every tenant gets the *same*
# vector for the same excerpt, byte for byte, which is what makes the M2.4 eval
# numbers a measurement of retrieval rather than of which machine ran the model.
SEED_VECTORS_FILE = SEED_FILE.with_name("gdpr_excerpts_vectors.json")

# The seed corpus is a source like any other and has to say so. M2.4's gold
# set keys on (source_key, chunk_ordinal); a NULL pair means a question about
# a seeded chunk scores zero against a corpus that is in fact sitting right
# there.
#
# The value is a literal shared with migration 0007, which subtracts it: an
# organisation holding only the seed has not "ingested" anything, and counting
# it would make INGEST_ORG_CAP refuse the very first ingestion. The two are
# held together by test_a_fresh_demo_org_does_not_count_against_the_ingest_cap;
# nothing else could notice them drifting apart.
SEED_SOURCE_KEY = "seed:gdpr-excerpts"

# One statement, both callers — `copy_corpus_into` directly and
# `seed_public_corpus` through it. That is not tidiness: it is what makes a
# later edit to the column list reach every tenant's corpus instead of only the
# public organisation's. A second copy of this INSERT is how one of them ends up
# writing NULL into a column the other populates, with nothing raised anywhere.
#
# Eight columns, not seven. `embedding` is Task 20's; `source_key` and
# `chunk_ordinal` are this task's. Dropping either back off the list does not
# raise — SQLAlchemy simply never binds the column that lost its writer — it
# just writes NULL into it for every tenant, which is the defect this
# statement is single-owner to prevent.
#
# `bindparam(..., type_=Vector(DIMENSIONS))` rather than a raw `list[float]` or
# a string-interpolated CAST. `register_vector` is called nowhere in this
# repository, so asyncpg holds no codec for `vector` and rejects a bare list
# outright: "invalid input for query argument $1: [...] (expected str, got
# list)". The typed bindparam hands the driver what it understands and keeps the
# value a bound parameter, with no SQL built by string construction.
#
# `DIMENSIONS` and not a literal 384, because `load_seed_vectors` below checks
# the file's width against `DIMENSIONS` too. Two spellings of one number are two
# sources of truth: change the constant and the guard stays green while this
# INSERT fails on width, with the error naming neither.
_INSERT_CHUNK = text(
    "INSERT INTO document_chunks "
    "(id, org_id, content, page_from, page_to, source_key, chunk_ordinal, embedding) "
    "VALUES (:id, :org_id, :content, :page_from, :page_to, :source_key, :chunk_ordinal, :embedding)"
).bindparams(bindparam("embedding", type_=Vector(DIMENSIONS)))


class CorpusUnavailableError(RuntimeError):
    """The seed corpus could not be read, so no tenant can be given one."""


@dataclass(frozen=True)
class Excerpt:
    content: str
    page_from: int
    page_to: int


@lru_cache(maxsize=1)
def load_excerpts() -> tuple[Excerpt, ...]:
    """Read the corpus file once per process.

    Cached because `provision_demo_org` calls this on every demo login, and
    re-reading and re-parsing a file to write the same rows would put disk I/O
    on the path of a request that already does two round trips to Postgres.

    The provenance of the text — source, URL, terms of reuse, how the excerpts
    were extracted — travels in the file itself rather than here, so it cannot
    drift away from the data it describes.

    Every way this can fail arrives as `CorpusUnavailableError` naming the path,
    which is the entire point of having the error: the file is found by walking
    up from `__file__`, so "which file did it actually look at" is the first
    question anyone will ask. Catching only `OSError` covered the missing file
    and left malformed JSON surfacing as a bare `JSONDecodeError` and a renamed
    key as a bare `KeyError` — two of the four failure modes bypassing the very
    error written to explain them.
    """
    try:
        document = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    except OSError as error:
        raise CorpusUnavailableError(f"cannot read the seed corpus at {SEED_FILE}") from error
    except json.JSONDecodeError as error:
        raise CorpusUnavailableError(f"the seed corpus at {SEED_FILE} is not valid JSON") from error

    try:
        excerpts = tuple(
            Excerpt(
                content=item["content"],
                page_from=int(item["page_from"]),
                page_to=int(item["page_to"]),
            )
            for item in document["excerpts"]
        )
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise CorpusUnavailableError(
            f"the seed corpus at {SEED_FILE} does not have the expected shape "
            f"({{'excerpts': [{{'content': str, 'page_from': int, 'page_to': int}}, ...]}}): "
            f"{error!r}"
        ) from error

    if not excerpts:
        raise CorpusUnavailableError(f"the seed corpus at {SEED_FILE} contains no excerpts")
    return excerpts


@lru_cache(maxsize=1)
def load_seed_vectors() -> tuple[tuple[float, ...], ...]:
    """Read the precomputed seed embeddings once per process.

    Keyed on the corpus file's own hash and on the model's name, not on trust.
    Both failures this guards are silent and they are different failures.

    Edit `gdpr_excerpts.json`, forget to re-run the generator, and every tenant
    gets vectors that describe the *previous* text. Edit `MODEL_NAME` instead
    and the hash still matches, the count still matches, the width is still 384
    — and the corpus is embedded in one vector space while the questions are
    embedded in another. Neither raises anything on its own; retrieval keeps
    working and keeps being wrong, which is worse than not working.

    Same error type and same loud-with-the-path style as `load_excerpts`, for
    the same reason: "which file did it actually read" is the first question.
    Both paths are named when the two disagree, because the answer is always
    "re-run `scripts/embed_seed_corpus.py`" and that only helps if the reader
    can see which pair drifted.

    Cached for the same reason `load_excerpts` is — this runs on every demo
    login — and cleared the same way in tests, which is load-bearing: a
    substituted `SEED_FILE` changes nothing while a previous test's value is
    still memoised, and the "proof" then proves only that the cache works.
    """
    try:
        document = json.loads(SEED_VECTORS_FILE.read_text(encoding="utf-8"))
    except OSError as error:
        raise CorpusUnavailableError(
            f"cannot read the seed embeddings at {SEED_VECTORS_FILE} — "
            f"run scripts/embed_seed_corpus.py"
        ) from error
    except json.JSONDecodeError as error:
        raise CorpusUnavailableError(
            f"the seed embeddings at {SEED_VECTORS_FILE} are not valid JSON"
        ) from error

    try:
        stored_model = str(document["model"])
        stored_sha256 = str(document["corpus_sha256"])
        dimensions = int(document["dim"])
        vectors = tuple(tuple(float(value) for value in vector) for vector in document["vectors"])
    except (KeyError, TypeError, ValueError) as error:
        raise CorpusUnavailableError(
            f"the seed embeddings at {SEED_VECTORS_FILE} do not have the expected shape "
            f"({{'model': str, 'dim': int, 'corpus_sha256': str, 'vectors': [[float, ...]]}}): "
            f"{error!r}"
        ) from error

    # Model drift, checked before anything else because it is the cheapest check
    # here and the one nothing else can see. `corpus_sha256` ties the vectors to
    # the *text*; nothing ties them to the *weights*. Change `MODEL_NAME` in
    # embedding.py — one line — and the hash still matches, the count still
    # matches and every vector is still 384 wide, while what ships is a corpus
    # embedded by one model being searched by questions embedded by another.
    # Retrieval does not break; it degrades, quietly, in a demo whose entire
    # claim is measurable retrieval quality.
    #
    # A string comparison, so it needs no model, no network and no cache and
    # therefore runs everywhere the loader runs — on a cold CI cache and on the
    # first demo login on the box. `test_the_committed_vectors_are_what_this_model
    # _produces_today` checks the stronger property (the numbers really came out
    # of those weights) but needs the weights to do it, so it is skipped exactly
    # when the risk is highest: right after someone changed the model.
    if stored_model != MODEL_NAME:
        raise CorpusUnavailableError(
            f"the seed embeddings at {SEED_VECTORS_FILE} were computed with {stored_model!r}, "
            f"but this process embeds questions with {MODEL_NAME!r} — the two vector spaces are "
            f"unrelated. Re-run scripts/embed_seed_corpus.py"
        )

    try:
        corpus_sha256 = hashlib.sha256(SEED_FILE.read_bytes()).hexdigest()
    except OSError as error:
        raise CorpusUnavailableError(f"cannot read the seed corpus at {SEED_FILE}") from error

    if corpus_sha256 != stored_sha256:
        raise CorpusUnavailableError(
            f"the seed embeddings at {SEED_VECTORS_FILE} were computed over a different "
            f"{SEED_FILE} (recorded {stored_sha256}, found {corpus_sha256}) — "
            f"re-run scripts/embed_seed_corpus.py"
        )

    excerpts = load_excerpts()
    if len(vectors) != len(excerpts):
        raise CorpusUnavailableError(
            f"the seed embeddings at {SEED_VECTORS_FILE} carry {len(vectors)} vectors for the "
            f"{len(excerpts)} excerpts in {SEED_FILE} — re-run scripts/embed_seed_corpus.py"
        )

    if dimensions != DIMENSIONS or any(len(vector) != DIMENSIONS for vector in vectors):
        raise CorpusUnavailableError(
            f"the seed embeddings at {SEED_VECTORS_FILE} are not {DIMENSIONS}-dimensional, "
            f"which document_chunks.embedding requires — re-run scripts/embed_seed_corpus.py"
        )

    return vectors


async def copy_corpus_into(session: AsyncSession, org_id: str) -> int:
    """Give `org_id` its own copy of the corpus. Returns the number of chunks.

    The session must already be scoped to `org_id`: `document_chunks` carries a
    WITH CHECK on every insert, so a row stamped with an organisation other than
    the session's context is rejected by Postgres rather than written.

    The session is a parameter rather than something opened here so the caller
    can put these rows in the same transaction as the organisation row itself.
    That is what `provision_demo_org` does, and it is the difference between
    "a demo organisation has a corpus" and "a demo organisation has a corpus
    unless the second statement failed".

    The rows arrive with their embeddings already on them, read from the file
    beside the corpus rather than computed here. No inference happens on this
    path — see `SEED_VECTORS_FILE` for why that matters on an API that runs one
    worker and holds this call inside `provision_demo_org`'s transaction.

    Not an `INSERT ... SELECT` from the public corpus organisation, which is the
    obvious way to avoid the file read and is silently wrong: `document_chunks`
    has FORCE row-level security and this session is scoped to `org_id`, so the
    source rows do not exist in this context. Every demo tenant would get an
    empty corpus and nothing would raise.

    `strict=True` on the zip even though `load_seed_vectors` has already checked
    the lengths against `load_excerpts`. It costs nothing and it catches a
    divergence introduced *after* that check — a substituted file, a cleared
    cache on one side only — which would otherwise write a short corpus.
    """
    excerpts = load_excerpts()
    await session.execute(
        _INSERT_CHUNK,
        [
            {
                "id": str(uuid.uuid4()),
                "org_id": org_id,
                "content": excerpt.content,
                "page_from": excerpt.page_from,
                "page_to": excerpt.page_to,
                # The ordinal is the excerpt's index in load_excerpts(), the
                # same order the seed vector list is keyed by — reordering
                # gdpr_excerpts.json silently reassigns every ordinal and
                # devalues the gold set built against the old order.
                "source_key": SEED_SOURCE_KEY,
                "chunk_ordinal": ordinal,
                "embedding": list(vector),
            }
            for ordinal, (excerpt, vector) in enumerate(
                zip(excerpts, load_seed_vectors(), strict=True)
            )
        ],
    )
    return len(excerpts)


async def seed_public_corpus() -> int:
    """Load the corpus into the permanent public organisation. Returns the count.

    Idempotent by replacement: the chunks are deleted and rewritten, so running
    this on every deployment leaves one copy rather than one more copy. The
    organisation row is created only if it is missing.

    All of it runs inside that organisation's own tenant context, which is what
    lets one transaction do both halves. `organizations` has an unconditional
    INSERT policy (`WITH CHECK (true)`, migration 0003) — that is what admits
    the very first row for any tenant — so a scoped session may create it; and
    `document_chunks` then accepts the chunks because the context already names
    their owner. Without the context the DELETE would match nothing, silently
    breaking idempotency, and the INSERT would be refused outright.

    No ON CONFLICT, and not for style. With an explicit conflict target Postgres
    runs the SELECT policy's qual as a conflict check, and this table's policies
    are per-command, so what that check does depends on the session's context
    rather than on the statement — a difference between a seeder that works and
    one that raises 42501 with nothing wrong. A unique violation is caught
    instead, which behaves the same way whatever the context.

    The savepoint is load-bearing: a unique violation aborts the enclosing
    transaction, so swallowing the exception without one leaves the session
    poisoned and the failure resurfaces at commit, pointing at the wrong line.

    **Stale rows are an operational fact, not a bug this can prevent.** These
    rows are a *copy* of `gdpr_excerpts.json` taken whenever this last ran, so a
    database seeded from an older file keeps serving the older text until
    someone runs it again — and nothing in the running application would notice,
    because nothing reads these rows. `copy_corpus_into` reads the file, so
    every demo tenant gets the current corpus whatever this organisation holds;
    the drift is invisible precisely because the seed organisation is not yet
    used for anything. It stops being invisible at M2, when these rows start
    carrying embeddings and the M2.4 eval keys on them — so re-running this
    belongs in the deployment that ships a changed corpus, and
    `test_the_seeded_rows_are_the_file_they_came_from` is what proves a run
    leaves the two in agreement.
    """
    org_id = str(PUBLIC_CORPUS_ORG_ID)
    async with tenant_session(org_id) as session:
        try:
            async with session.begin_nested():
                await session.execute(
                    text(
                        "INSERT INTO organizations (id, external_id, name, expires_at) "
                        "VALUES (:id, 'seed', 'Public corpus', NULL)"
                    ),
                    {"id": org_id},
                )
        except IntegrityError:
            pass  # already created; the chunk refresh below is the idempotent part

        await session.execute(
            text("DELETE FROM document_chunks WHERE org_id = :org_id"), {"org_id": org_id}
        )
        return await copy_corpus_into(session, org_id)
