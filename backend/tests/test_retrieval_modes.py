"""HYBRID and the RRF fusion it is built from.

Two properties, each stated so it cannot pass by coincidence:

* `test_vector_mode_answers_a_question_that_shares_no_word_with_the_corpus`
  shows `HYBRID` does not lose the vector leg when the lexical leg is empty —
  the thing vector search was added for, not an assertion that two orderings
  merely differ (that would be a coin flip over thirty seeded chunks).
* `test_a_chunk_both_legs_agree_on_outranks_one_that_only_leads_the_lexical_leg`
  shows `reciprocal_rank_fusion` fuses by rank, never by score.
"""

import uuid

import pytest
from pgvector.sqlalchemy import Vector
from sqlalchemy import bindparam, text

from app.chat.embedding import DIMENSIONS, get_embedder
from app.chat.retrieval import SearchMode, reciprocal_rank_fusion, search_chunks
from app.db.session import system_session, tenant_session

_CLAUSE = "The processor shall notify the controller within 72 hours of becoming aware."

# Same typed bindparam as Task 20's SQL. asyncpg has no codec for `vector` and
# rejects a bare list with "expected str, got list"; the type is what makes
# this a bind parameter rather than a string built into the statement.
_SET_EMBEDDING = text("UPDATE document_chunks SET embedding = :vec WHERE id = :id").bindparams(
    bindparam("vec", type_=Vector(DIMENSIONS))
)


async def _new_org(created_orgs: list[str], name: str = "Hybrid fixture") -> str:
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


async def _embed(org_id: str) -> None:
    """Embed every chunk an organisation currently owns, in place.

    The same shape as the backfill task: read the unembedded rows, embed their
    content and write the vector back with a typed `bindparam`. No model
    dependency is hidden here — the caller marks the test `@pytest.mark.model`.
    """
    async with tenant_session(org_id) as session:
        rows = (
            await session.execute(
                text("SELECT id, content FROM document_chunks WHERE org_id = :org"),
                {"org": org_id},
            )
        ).mappings().all()
        for row in rows:
            vector = get_embedder().encode([row["content"]])[0]
            await session.execute(_SET_EMBEDDING, {"vec": vector, "id": row["id"]})


@pytest.mark.model
async def test_vector_mode_answers_a_question_that_shares_no_word_with_the_corpus(
    created_orgs: list[str],
) -> None:
    """The property a mode switch has. Revision 1 asserted a coincidence instead.

    Revision 1 ran the same question in both modes and asserted the two orderings
    differ. Over thirty seeded chunks and five returned rows that is a coin flip
    with good odds, not a property: one question on which both rank the same
    article first turns it red with nothing broken, and it re-rolls itself every
    time the corpus changes — which M2.2 does, twentyfold.

    What is necessarily true is the thing vector search was added for. The FTS
    branch matches lexemes, so a question written in words the corpus does not
    use matches nothing at all: `WHERE question.query IS NOT NULL AND ... @@
    question.query` at retrieval.py:88-89 admits no row. The vector branch orders
    by distance and has no threshold, so with one embedded chunk present it
    always returns it. Empty versus non-empty, same question, same tenant, same
    transaction — and nothing in that depends on which article ranks higher.
    """
    org = await _new_org(created_orgs)
    await _seed_chunk(org, _CLAUSE)
    await _embed(org)

    # Not one shared stem with the clause: "leak" is not "breach", "regulator" is
    # not "supervisory authority", "how soon" is not "72 hours".
    question = "How soon must a leak be escalated to the regulator?"

    async with tenant_session(org) as session:
        lexical = await search_chunks(session, question, mode=SearchMode.FTS)
        semantic = await search_chunks(session, question, mode=SearchMode.VECTOR)
        fused = await search_chunks(session, question, mode=SearchMode.HYBRID)

    assert lexical == [], "no lexeme is shared, so the lexical leg must find nothing"
    assert semantic, "an embedded chunk always has a nearest neighbour; there is no threshold"
    assert fused, "hybrid must not lose the vector leg when the lexical leg is empty"
    # Fusion re-orders what the legs found and invents nothing. This is what
    # fails if the hybrid branch quietly runs a third query of its own.
    assert {c["chunk_id"] for c in fused} <= {c["chunk_id"] for c in lexical + semantic}


def test_a_chunk_both_legs_agree_on_outranks_one_that_only_leads_the_lexical_leg() -> None:
    """The property that separates rank fusion from score fusion.

    A weighted sum of ts_rank and cosine similarity puts `only_lexical` first:
    ts_rank is unbounded, so one strong lexical hit outweighs any bounded
    similarity, and the constant that would stop it is a number tuned until the
    demo looked right. RRF never sees a score. 1/62 + 1/61 = 0.0325 beats
    1/61 = 0.0164, so agreement across both legs wins over leading one of them.
    """
    only_lexical, agreed = uuid.uuid4(), uuid.uuid4()

    order = reciprocal_rank_fusion([[only_lexical, agreed], [agreed]])

    assert order[0] == agreed
    assert set(order) == {only_lexical, agreed}, "fusion returns what it was given, nothing else"
