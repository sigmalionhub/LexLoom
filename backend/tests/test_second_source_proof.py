"""The proof that a second document does what the demo claims it does.

**The ground truth in this file was fixed before the run, and derived from the
publisher's own document rather than from what retrieval returned.** That is not
a formality: choosing the question after seeing which chunks come back is how a
retrieval demo proves nothing at all while looking convincing, and this
repository's own gold set was re-bound once under exactly that rule.

Recorded in advance, from `app/ingest/sources/nist-sp-800-63b.src.html`:

  question  "What cryptography must an AAL3 authenticator use?"
  sentence  "Authenticators used at AAL3 SHALL use approved cryptography."
            — present in exactly 1 of the document's 746 chunks
  section   Authentication Assurance Levels
            > Authentication Assurance Level 3
            > Authenticator and Verifier Requirements

**Why this is a real negative control.** An earlier form of this proof asked
one question of two tenants and compared the answers, which proves very little:
Postgres full-text search ORs the question's lexemes, so a tenant that has never
seen this document still returns *something* from the GDPR. Measured on the
committed corpora: the words `AAL3`, `authenticator` and `approved cryptography`
appear in **0** of the GDPR's 978 chunks. So the assertion is not "the other
tenant does worse" — it is that the other tenant returns **no citation from this
document at all**, which an OR of lexemes cannot satisfy by accident.

Four assertions, and all four have to hold:

  (a) the ingesting tenant's top citation comes from `nist-sp-800-63b`
  (b) its content carries the recorded sentence intact — routing is not enough,
      because a mangled chunk still matches the keywords, which is precisely the
      failure `pypdf` produced on the other document
  (c) its `locator` names the recorded section
  (d) the other tenant returns no citation from that source
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.chat.retrieval import SearchMode, search_chunks
from app.db.session import tenant_session
from app.ingest.jobs import run_ingest

pytestmark = [pytest.mark.model, pytest.mark.timeout(300)]

SOURCE_KEY = "nist-sp-800-63b"
QUESTION = "What cryptography must an AAL3 authenticator use?"
SENTENCE = "Authenticators used at AAL3 SHALL use approved cryptography."
SECTION = (
    "Authentication Assurance Levels › Authentication Assurance Level 3 "
    "› Authenticator and Verifier Requirements"
)


async def test_only_the_tenant_that_ingested_can_cite_the_second_document(
    created_orgs,
) -> None:
    from test_ingest_job import _make_org  # noqa: PLC2701 - shared fixture helper

    ingesting = await _make_org(created_orgs)
    untouched = await _make_org(created_orgs)

    # Both tenants get the seed corpus, so a failure of isolation shows up as a
    # WRONG citation rather than as no citation — the construction the RLS tests
    # use, and the one that makes a fail-open read as a defect.
    from app.chat.corpus import copy_corpus_into

    for org_id in (ingesting, untouched):
        async with tenant_session(org_id) as session:
            await copy_corpus_into(session, org_id)

    written = await run_ingest(ingesting, SOURCE_KEY)
    assert written == 746

    async with tenant_session(ingesting) as session:
        cited = await search_chunks(session, QUESTION, mode=SearchMode.FTS, limit=5)

    assert cited, "the ingesting tenant matched nothing, so nothing below means anything"
    top = cited[0]

    # (a) it comes from the document that was ingested
    assert top["source_key"] == SOURCE_KEY

    # (b) the text survived the whole pipeline — HTML preprocessing, LibreOffice,
    #     python-docx, chunking — intact. Routing to the right document proves
    #     the plumbing; this proves the corpus.
    assert SENTENCE in str(top["content"])

    # (c) the citation says where in the document, in words a reader can find
    assert top["locator"] == SECTION
    assert top["page_from"] is None

    # (d) the negative control
    async with tenant_session(untouched) as session:
        others = await search_chunks(session, QUESTION, mode=SearchMode.FTS, limit=20)
    assert all(c["source_key"] != SOURCE_KEY for c in others)


async def test_the_recorded_sentence_is_unique_in_the_document(created_orgs) -> None:
    """The premise of assertion (b), checked rather than assumed.

    If the sentence appeared in several chunks, `SENTENCE in content` would pass
    for a chunk from the wrong section and the locator assertion would be doing
    all the work alone.
    """
    from app.ingest.catalogue import CATALOGUE
    from app.ingest.parse import parse_source

    chunks = parse_source(CATALOGUE[SOURCE_KEY])
    carrying = [c for c in chunks if SENTENCE in c.content]
    assert len(carrying) == 1
    assert carrying[0].locator == SECTION


async def test_the_other_document_cannot_answer_this_question(created_orgs) -> None:
    """Why (d) is a control and not a coincidence.

    FTS ORs the question's lexemes, so "the other tenant answered worse" would
    be satisfied by any corpus at all. This measures the stronger claim: the
    terms are simply not in the GDPR.
    """
    from app.ingest.catalogue import CATALOGUE
    from app.ingest.parse import parse_source

    gdpr = " ".join(c.content for c in parse_source(CATALOGUE["gdpr-full"])).lower()
    for term in ("aal3", "authenticator", "approved cryptography"):
        assert term not in gdpr


async def test_the_seed_corpus_is_thirty_excerpts(created_orgs) -> None:
    """The number the panel tells a visitor, read from the file rather than
    written twice. A literal in the frontend would be a plausible figure in a
    readout styled like a measurement, which is the habit that panel exists to
    avoid."""
    org_id = await _seeded(created_orgs)
    async with tenant_session(org_id) as session:
        count = await session.scalar(
            text("SELECT count(*) FROM document_chunks WHERE source_key = 'seed:gdpr-excerpts'")
        )
    assert count == 30


async def _seeded(created_orgs) -> str:
    from test_ingest_job import _make_org  # noqa: PLC2701

    from app.chat.corpus import copy_corpus_into

    org_id = await _make_org(created_orgs)
    async with tenant_session(org_id) as session:
        await copy_corpus_into(session, org_id)
    return org_id
