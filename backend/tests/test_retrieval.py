import uuid

from sqlalchemy import text

from app.chat.retrieval import SearchMode, search_chunks
from app.db.session import system_session, tenant_session

_CLAUSE = "The processor shall notify the controller within 72 hours."


async def _new_org(created_orgs: list[str], name: str = "Retrieval fixture") -> str:
    """Seed an organisation and register it for teardown.

    Through `system_session` because `organizations` admits an insert from a
    caller with no tenant context (migration 0003, `WITH CHECK (true)`) — which
    is the only way the first row for a new tenant can ever be written.
    """
    org_id = str(uuid.uuid4())
    async with system_session() as session:
        await session.execute(
            text("INSERT INTO organizations (id, external_id, name) VALUES (:i, :e, :n)"),
            {"i": org_id, "e": f"ext_{org_id}", "n": name},
        )
    created_orgs.append(org_id)
    return org_id


async def _seed_chunk(org_id: str, content: str, page: int = 1) -> None:
    async with tenant_session(org_id) as session:
        await session.execute(
            text(
                "INSERT INTO document_chunks (id, org_id, content, page_from, page_to) "
                "VALUES (:i, :o, :c, :p, :p)"
            ),
            {"i": str(uuid.uuid4()), "o": org_id, "c": content, "p": page},
        )


async def test_a_question_written_as_a_question_finds_the_clause_that_answers_it(
    created_orgs: list[str],
) -> None:
    """The property this retrieval rewrite exists for.

    A substring match answers this with nothing — no chunk contains the sentence
    "What are the processor's obligations?" — and so does `plainto_tsquery`,
    which ANDs every content word and therefore demands one chunk carry all of
    them. Both produced an empty `citations` event on the live demo while
    raising nothing anywhere, which is the failure this whole task existed to
    remove.
    """
    org = await _new_org(created_orgs)
    await _seed_chunk(org, _CLAUSE)
    async with tenant_session(org) as session:
        hits = await search_chunks(
            session, "What are the processor's obligations?", mode=SearchMode.FTS
        )
    assert len(hits) == 1
    assert "notify the controller" in hits[0]["content"]


async def test_a_question_matches_on_any_of_its_terms_rather_than_all_of_them(
    created_orgs: list[str],
) -> None:
    """OR, not AND — asserted by seeding the terms in *different* chunks.

    This is the exact shape `plainto_tsquery` gets wrong: with "processor" in
    one chunk and "supervisory authority" in another, an AND query matches
    neither and the question goes uncited. Both chunks come back here, which is
    what a reader needs in order to see where an answer came from.
    """
    org = await _new_org(created_orgs)
    await _seed_chunk(org, "The processor acts only on documented instructions.", page=1)
    await _seed_chunk(org, "The supervisory authority must be told within 72 hours.", page=2)
    async with tenant_session(org) as session:
        hits = await search_chunks(
            session, "must the processor tell the supervisory authority?", mode=SearchMode.FTS
        )
    assert {hit["page_from"] for hit in hits} == {1, 2}


async def test_the_best_match_comes_first_and_carries_the_higher_score(
    created_orgs: list[str],
) -> None:
    """Ranking is why `score` stopped being a hardcoded 1.0.

    A chunk matching two of the question's terms outranks one matching a single
    term, and the `score` the API reports is the value the rows were ordered by
    — not a constant travelling alongside them, which is what it was while the
    documented contract already called it a score.
    """
    org = await _new_org(created_orgs)
    await _seed_chunk(org, "Encryption and pseudonymisation of personal data.", page=1)
    await _seed_chunk(org, "Encryption of backup media is required.", page=2)
    async with tenant_session(org) as session:
        hits = await search_chunks(session, "encryption and pseudonymisation", mode=SearchMode.FTS)

    assert len(hits) == 2
    assert hits[0]["page_from"] == 1, "the chunk matching both terms should rank first"
    assert hits[0]["score"] > hits[1]["score"] > 0
    scores = [hit["score"] for hit in hits]
    assert scores == sorted(scores, reverse=True), (
        "the reported scores are not the order the rows arrived in"
    )


async def test_a_question_finds_a_chunk_that_uses_a_different_form_of_the_word(
    created_orgs: list[str],
) -> None:
    """Stemming is what makes this a search rather than a string comparison: the
    corpus says "obligations", the visitor types "obligation"."""
    org = await _new_org(created_orgs)
    await _seed_chunk(org, "The obligations of the processor survive termination.")
    async with tenant_session(org) as session:
        assert len(await search_chunks(session, "processor obligation", mode=SearchMode.FTS)) == 1


async def test_a_word_whose_stem_would_stem_again_is_still_found(
    created_orgs: list[str],
) -> None:
    """The query is *cast* to tsquery, not passed through `to_tsquery`.

    `to_tsquery` runs the dictionary, and the lexemes handed to it have already
    been through it once — so it stems the stems. `pseudonymis` becomes
    `pseudonymi` and `eras` becomes `era`, neither of which is in any tsvector,
    and the corpus term becomes unfindable with nothing raised anywhere.
    Measured over the seeded GDPR corpus when this was first written: 21 of its
    276 distinct lexemes were altered by that second pass.

    Both words below are in that set. Under the double-stemming version this
    test returns nothing twice; the wording of every question here is ordinary,
    so nothing about it looks like a trap.
    """
    org = await _new_org(created_orgs)
    await _seed_chunk(org, "Pseudonymisation of personal data is required.", page=1)
    await _seed_chunk(org, "The erasure of personal data must be without undue delay.", page=2)
    async with tenant_session(org) as session:
        assert len(await search_chunks(session, "pseudonymisation", mode=SearchMode.FTS)) == 1
        assert len(
            await search_chunks(session, "when is erasure required?", mode=SearchMode.FTS)
        ) == 2


async def test_a_question_of_nothing_but_stop_words_matches_nothing(
    created_orgs: list[str],
) -> None:
    """"the and or of" has no lexemes, so `string_agg` returns NULL.

    Without the `IS NOT NULL` guard in the statement this is the failure that
    would not look like one. A NULL `tsquery` is not an empty one, and "matches
    nothing" is one WHERE-clause rearrangement away from "matches everything" —
    at which point the endpoint cites the tenant's entire corpus as the source
    of an answer it had nothing to do with.
    """
    org = await _new_org(created_orgs)
    await _seed_chunk(org, _CLAUSE)
    async with tenant_session(org) as session:
        assert await search_chunks(session, "the and or of", mode=SearchMode.FTS) == []
        assert await search_chunks(session, "", mode=SearchMode.FTS) == []
        assert await search_chunks(session, "%", mode=SearchMode.FTS) == []


async def test_tsquery_operators_in_the_question_are_words_and_not_operators(
    created_orgs: list[str],
) -> None:
    """A question is data. `to_tsquery` would read it as a program.

    `&`, `|`, `!` and `<->` are tsquery syntax, so a question handed to
    `to_tsquery` raw rewrites its own query — `processor & !controller` would
    *exclude* the chunk it should find — and an unbalanced parenthesis makes the
    endpoint raise instead of answer. The lexemes come from `to_tsvector` first,
    which reduces every operator to an ordinary word or drops it.

    The control matters as much as the assertion: the same terms without the
    operators find the same chunk, so this is about the operators and not about
    the seeding.
    """
    org = await _new_org(created_orgs)
    await _seed_chunk(org, _CLAUSE)
    async with tenant_session(org) as session:
        assert len(
            await search_chunks(session, "processor & !controller", mode=SearchMode.FTS)
        ) == 1
        assert len(
            await search_chunks(session, "notify <-> controller | (((", mode=SearchMode.FTS)
        ) == 1
        assert len(await search_chunks(session, "processor controller", mode=SearchMode.FTS)) == 1


async def test_a_question_that_looks_like_sql_is_still_only_a_question(
    created_orgs: list[str],
) -> None:
    """Quotes and semicolons reach `quote_literal`, which is where they stop.

    The lexemes are interpolated into a `tsquery` string, so that is the one
    place in this function where a value becomes syntax. `quote_literal` keeps
    it a value: without it a lexeme carrying a quote would close its own term
    and everything after it would be parsed as query syntax.
    """
    org = await _new_org(created_orgs)
    await _seed_chunk(org, _CLAUSE)
    async with tenant_session(org) as session:
        # 'drop', 'tabl' and 'document'/'chunk' are lexemes here, and none of
        # them is in the seeded clause — so nothing matches, and nothing runs.
        assert (
            await search_chunks(session, "'; DROP TABLE document_chunks; --", mode=SearchMode.FTS)
            == []
        )
        # The table is still there, and still holds its row.
        assert len(await search_chunks(session, "notify the controller", mode=SearchMode.FTS)) == 1


async def test_a_question_longer_than_the_limit_still_finds_its_chunk(
    created_orgs: list[str],
) -> None:
    """Truncation bounds the lexing work and must not cost the answer.

    The words that identify the chunk are inside the first 200 characters, so a
    long tail does not stop it being found — it simply contributes no lexemes.
    """
    org = await _new_org(created_orgs)
    await _seed_chunk(org, _CLAUSE)
    question = "processor notify controller " + "padding " * 60
    assert len(question) > 200, "the question has to exceed the cut for this to test anything"
    async with tenant_session(org) as session:
        assert len(await search_chunks(session, question, mode=SearchMode.FTS)) == 1


async def test_the_limit_bounds_what_comes_back(created_orgs: list[str]) -> None:
    """The SSE payload carries every citation, so an unbounded query would put a
    tenant's whole corpus on the wire in answer to one question."""
    org = await _new_org(created_orgs)
    for page in range(1, 8):
        await _seed_chunk(org, f"The processor has an obligation on page {page}.", page=page)
    async with tenant_session(org) as session:
        assert len(await search_chunks(session, "processor obligation", mode=SearchMode.FTS)) == 5
        assert len(
            await search_chunks(session, "processor obligation", mode=SearchMode.FTS, limit=3)
        ) == 3


async def test_search_chunks_never_sees_another_tenants_chunk(
    created_orgs: list[str],
) -> None:
    """The function carries no `WHERE org_id`, so this is the test its docstring
    points at: the boundary is the RLS policy, asserted through the real query
    rather than assumed from the absence of a filter."""
    mine = await _new_org(created_orgs, "Acme Legal")
    theirs = await _new_org(created_orgs, "Globex Compliance")
    await _seed_chunk(mine, "Acme obligations under clause 4.")
    await _seed_chunk(theirs, "Globex obligations under clause 4.")

    async with tenant_session(mine) as session:
        hits = await search_chunks(session, "obligations under clause 4", mode=SearchMode.FTS)

    assert [hit["org_id"] for hit in hits] == [mine]

async def _seed_identified_chunk(
    org_id: str, content: str, source_key: str, ordinal: int, page: int = 1
) -> None:
    async with tenant_session(org_id) as session:
        await session.execute(
            text(
                "INSERT INTO document_chunks "
                "(id, org_id, content, page_from, page_to, source_key, chunk_ordinal) "
                "VALUES (:i, :o, :c, :p, :p, :s, :n)"
            ),
            {
                "i": str(uuid.uuid4()),
                "o": org_id,
                "c": content,
                "p": page,
                "s": source_key,
                "n": ordinal,
            },
        )

async def test_chunks_with_the_same_score_come_back_in_a_stable_order(
    created_orgs: list[str],
) -> None:
    """Equal scores must not mean an arbitrary order, and that is not cosmetic.

    `ts_rank` returns `real`, and on a single document most rows score exactly
    alike: measured over the eval gold set's thirty questions, 288 of the 570
    adjacent pairs in the top-20 lists carried byte-identical scores. Whatever
    order the plan happened to emit inside those groups was the order every
    retrieval metric then read, so permuting only the ties moved `fts` MRR@10 by
    up to 0.022 and `hybrid` MRR@10 by up to 0.040 — drift indistinguishable
    from a retrieval regression, and the reason `app/evals/floors.json` can
    carry a threshold at all.

    Identical content, so the scores are identical by construction; inserted in
    the reverse of the expected order, so passing cannot be an accident of
    insertion order. The tie-break is `(source_key, chunk_ordinal)` and not `id`
    — see the comment on `_SEARCH_SQL`.
    """
    org = await _new_org(created_orgs)
    body = "The controller shall document every processing activity."
    for ordinal in (4, 2, 0, 3, 1):
        await _seed_identified_chunk(org, body, "gdpr-full", ordinal)
    await _seed_identified_chunk(org, body, "a-earlier-source", 9)

    async with tenant_session(org) as session:
        hits = await search_chunks(session, "controller document processing", mode=SearchMode.FTS)
        again = await search_chunks(session, "controller document processing", mode=SearchMode.FTS)

    assert len({hit["score"] for hit in hits}) == 1, "the fixture must produce a genuine tie"
    assert [hit["chunk_id"] for hit in hits] == [hit["chunk_id"] for hit in again]
    async with tenant_session(org) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, source_key, chunk_ordinal FROM document_chunks "
                    "WHERE org_id = :o"
                ),
                {"o": org},
            )
        ).mappings().all()
    identity = {str(row["id"]): (row["source_key"], row["chunk_ordinal"]) for row in rows}
    ordered = [identity[hit["chunk_id"]] for hit in hits]
    assert ordered == sorted(ordered), (
        "tied rows did not come back in (source_key, chunk_ordinal) order; the ORDER BY "
        "lost its total order and every eval metric is arbitrary again"
    )
