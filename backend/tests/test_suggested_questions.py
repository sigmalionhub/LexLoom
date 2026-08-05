"""Every question the page offers has to be a question the corpus answers.

A suggestion is a promise: the page puts it in front of a visitor as something
worth clicking, and the visitor's first impression of this whole demo is what
comes back. A suggestion that retrieves nothing renders "No passage in this
tenant's corpus matched" — honest, and a terrible first answer. One that
retrieves out of the *other* document is worse: it looks like it worked.

So the suggestions live in the catalogue beside the document they belong to,
and this file is why that is worth the schema field. The three NIST questions
were not written from a reading of the document — twelve candidates were put
through `search_chunks` and these are the three whose returned passage actually
answered the question. This test re-runs that check against the committed
catalogue, so a question edited later without re-measuring fails here.

The tenant holds BOTH documents, which is what makes the assertion mean
anything: FTS ORs the question's lexemes, so a tenant holding only one source
returns hits from it almost regardless of what was asked.
"""

from __future__ import annotations

import pytest

from app.chat.retrieval import SearchMode, search_chunks
from app.db.session import tenant_session
from app.ingest.catalogue import CATALOGUE
from app.ingest.jobs import run_ingest

pytestmark = [pytest.mark.model, pytest.mark.timeout(600)]


async def test_every_suggested_question_retrieves_from_its_own_document(
    created_orgs,
) -> None:
    from test_ingest_job import _make_org  # noqa: PLC2701 - shared fixture helper

    from app.chat.corpus import copy_corpus_into

    suggesting = {key: source for key, source in CATALOGUE.items() if source.suggestions}
    assert suggesting, "no source offers a question, so this test proves nothing"

    org_id = await _make_org(created_orgs)
    async with tenant_session(org_id) as session:
        await copy_corpus_into(session, org_id)

    # Every catalogue source, not only the suggesting ones: a question must beat
    # the rest of the tenant's corpus, not merely exist in isolation.
    for key, source in CATALOGUE.items():
        written = await run_ingest(org_id, key)
        assert written == source.expected_chunks

    for key, source in suggesting.items():
        for question in source.suggestions:
            async with tenant_session(org_id) as session:
                hits = await search_chunks(session, question, mode=SearchMode.FTS, limit=5)

            assert hits, f"{key}: {question!r} retrieved nothing"
            assert hits[0]["source_key"] == key, (
                f"{key}: {question!r} answered out of "
                f"{hits[0]['source_key']!r}, which is not the document that "
                f"offered it"
            )


def test_a_source_with_no_pages_still_offers_a_findable_position() -> None:
    """The suggestions are only useful if the citation can be looked up.

    `nist-sp-800-63b` has no page numbers, so every one of its citations shows
    `locator` instead. A suggestion pointing at a passage a reader cannot find
    in the published document would be a citation in name only.
    """
    source = CATALOGUE["nist-sp-800-63b"]
    assert source.suggestions
    assert source.pages is None
    assert source.source_url.startswith("https://pages.nist.gov/")


def test_the_seeded_source_offers_none() -> None:
    """`gdpr-full` deliberately has no suggestions, and that is not an oversight.

    Every tenant is seeded with 30 excerpts of the GDPR, so the page can offer
    GDPR questions from the first paint — it does, in `components/chat.tsx`.
    Catalogue suggestions are for what a tenant does *not* hold until it asks
    for it. If this ever changes, the questions added here must be ones the seed
    cannot answer, or the ingest button will look like it did nothing.
    """
    assert CATALOGUE["gdpr-full"].suggestions == ()
