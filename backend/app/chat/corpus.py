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

import json
import pathlib
import uuid
from dataclasses import dataclass
from functools import lru_cache

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

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

_INSERT_CHUNK = text(
    "INSERT INTO document_chunks (id, org_id, content, page_from, page_to) "
    "VALUES (:id, :org_id, :content, :page_from, :page_to)"
)


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
    """
    try:
        document = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    except OSError as error:
        raise CorpusUnavailableError(f"cannot read the seed corpus at {SEED_FILE}") from error

    excerpts = tuple(
        Excerpt(
            content=item["content"],
            page_from=int(item["page_from"]),
            page_to=int(item["page_to"]),
        )
        for item in document["excerpts"]
    )
    if not excerpts:
        raise CorpusUnavailableError(f"the seed corpus at {SEED_FILE} contains no excerpts")
    return excerpts


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
            }
            for excerpt in excerpts
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
