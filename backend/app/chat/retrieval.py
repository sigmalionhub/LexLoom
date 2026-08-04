from enum import StrEnum

from pgvector.sqlalchemy import Vector
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.embedding import DIMENSIONS, get_embedder

Citation = dict[str, object]


class SearchMode(StrEnum):
    """Which retrieval path a question takes.

    The values are the ones stored in `messages.search_mode` (migration 0005, a
    `varchar(16)`) and the ones the frontend puts in the URL, so the enum, the
    column and the query string all spell a mode the same way. `StrEnum` because
    the value crosses a text column and a JSON body and has to survive both
    without a conversion table.

    Deliberately not carrying a default anywhere. `search_chunks` takes `mode`
    keyword-only and without a default, so a call site that forgot to read the
    stored choice is a type error rather than a search that quietly runs in
    `FTS` — which is the inert-toggle failure this milestone exists to remove.

    `HYBRID` is task 21's member; it arrives with the RRF branch that gives it
    meaning, because an enum member no code path implements is a mode a visitor
    can select and never receive.
    """

    FTS = "fts"
    VECTOR = "vector"

# The text search configuration, named once and bound as a parameter rather
# than interpolated. Every `to_tsvector` below has to use the same one: lexing
# the question with a different dictionary than the corpus produces stems that
# cannot match, and that fails by finding nothing rather than by raising.
#
# A parameter and not an f-string, so this file contains no SQL built by string
# construction at all. The value is a module constant today and interpolating it
# would be safe today; the next person to make it configurable is the problem,
# and they would be editing a line that already looked fine.
_TEXT_SEARCH_CONFIG = "english"

# Questions are truncated rather than rejected. The bound is on the work the
# database is asked to do — every character reaches `to_tsvector`, and the
# resulting query carries one lexeme per distinct non-stop word — not on what a
# person is allowed to ask. 200 characters is a long question.
_MAX_QUERY_CHARS = 200

# The question becomes the OR of its own lexemes, ranked.
#
# Three shapes were measured against the seeded GDPR corpus before this one was
# chosen, because the two obvious ones both answer a real question with nothing:
#
#   ILIKE '%…%'      "What are the processor's obligations?" -> 0 rows. A
#                    question is not a substring of any one chunk, so the
#                    demo emitted an empty `citations` event and looked broken.
#   plainto_tsquery  -> 0 rows as well. It ANDs every content word, so a
#                    question whose terms are spread across the corpus matches
#                    no single chunk.
#   OR of lexemes    -> Article 28(3), the processor-obligations article,
#                    ranked first; "what must be done within 72 hours" returns
#                    Article 33(1), the breach-notification one.
#
# The query is built from the lexemes Postgres itself produced, never from the
# raw question. That is what makes this safe: `&`, `|`, `!` and `<->` are
# tsquery operators, so a query assembled from user text would let a question
# rewrite itself — and an unbalanced parenthesis would make the endpoint raise
# rather than answer. Running the question through `to_tsvector` first reduces
# every one of those to an ordinary lexeme or drops it. Measured: `a & b | !c
# <-> d` yields the query `'b' | 'c' | 'd'`.
#
# The assembled string is **cast** to tsquery rather than passed through
# `to_tsquery`, and the difference is not cosmetic. `to_tsquery` runs the
# dictionary, so it stems what `to_tsvector` already stemmed: it turns the
# lexeme `pseudonymis` into `pseudonymi`, which then matches nothing, and the
# corpus term is unfindable with no error anywhere. Measured over the seeded
# GDPR corpus: 21 of its 276 distinct lexemes are altered by that second pass.
# The cast parses tsquery syntax without consulting the dictionary, which is
# exactly right for input that is already dictionary output.
#
# Escaping is therefore load-bearing, and it is written out rather than left to
# `quote_literal`. A quoted tsquery lexeme escapes both the quote and the
# backslash by doubling; `quote_literal` doubles the quote but switches to the
# `E'…'` form as soon as it sees a backslash, and `E'…'` is not tsquery syntax.
# Lexemes really can carry either character — the default parser emits file
# paths and URLs as single tokens — and an unescaped quote is a hard syntax
# error, so this is a 500 rather than a silent miss.
#
# A question of only stop words yields no lexemes at all, `string_agg` returns
# NULL, and the NULL guard is what makes that no rows rather than every row.
#
# No GIN index on `to_tsvector(content)`, deliberately. A tenant owns tens of
# chunks, so scanning its own rows is the cheaper plan and an index would be
# maintained for no gain. An index is also a relation, and the catalog inventory
# in tests/test_demo_orgs.py pins every relation by name on purpose — so when M2
# brings corpora large enough to need one, the index and the inventory entry go
# in together.
_SEARCH_SQL = text(
    r"""
    WITH question AS (
        SELECT string_agg(
                   '''' || replace(replace(lexeme, '\', '\\'), '''', '''''') || '''',
                   ' | '
               )::tsquery AS query
        FROM unnest(to_tsvector(CAST(:config AS regconfig), :question))
    )
    SELECT id,
           org_id,
           content,
           page_from,
           page_to,
           ts_rank(to_tsvector(CAST(:config AS regconfig), content), question.query) AS score
    FROM document_chunks, question
    WHERE question.query IS NOT NULL
      AND to_tsvector(CAST(:config AS regconfig), content) @@ question.query
    ORDER BY score DESC, page_from
    LIMIT :limit
    """
)

# The same columns as the lexical statement, in the same order, so both branches
# feed one projection and neither can quietly grow or lose a field.
#
# `org_id` is in the select list on purpose and is not decoration:
# `test_retrieval_scoping.py` reads it to show a tenant sees only its own rows,
# and `:71` asserts the *key is present* precisely so that dropping it turns
# those checks into KeyErrors rather than into assertions about nothing. The
# tempting fix when they go red is to delete them.
#
# `1 - (embedding <=> :qvec)` — `<=>` is cosine distance, so this is cosine
# similarity: bounded, and therefore meaningful across questions in a way
# `ts_rank` is not. The operator is schema-qualified for the same reason
# migration 0004 qualifies its own: `search_path` is not part of the contract,
# and an operator resolved out of a different schema is a silent change of
# meaning rather than an error.
#
# `.bindparams(... type_=Vector(DIMENSIONS))`, never `CAST(:qvec AS
# public.vector)`. `register_vector` is called nowhere in this repository, so
# asyncpg has no codec for `vector` and rejects a bare `list[float]` with
# "invalid input for query argument $1: [...] (expected str, got list)". The
# typed bindparam hands the driver a value it understands and keeps it a bound
# parameter. `DIMENSIONS` rather than a literal 384, so the width this query is
# bound at and the width the embedder produces cannot drift apart by one edit.
#
# `WHERE embedding IS NOT NULL` filters rather than relying on the sort. The
# column is nullable — a chunk ingested before `lexloom.embed_org_corpus` ran
# has no vector — and `NULL <=> v` is NULL, which is not "far away": it is
# unordered. Postgres puts it last for an ascending sort today, so an unembedded
# chunk only surfaces once a tenant owns fewer than `limit` embedded ones, which
# is exactly the state a new tenant is in. It would then arrive with a NULL
# score, and `float(None)` raises — a 500 on the demo's first question, from a
# clause that looked like a formality. Filtering says what is meant: a chunk
# with no vector is not a candidate for a vector search.
#
# No HNSW index here, and that is a decision rather than an omission. A tenant
# owns tens of chunks today and ~600 after M2.2; an exact scan of 600x384 floats
# is a fraction of a millisecond, and the tenant's own rows are already reachable
# through `ix_document_chunks_org`. More importantly, under row-level security an
# approximate index makes the answers *worse*: it returns the `ef_search` nearest
# rows across the whole table, the policy then discards every other tenant's, and
# the caller gets fewer rows than it asked for. The full argument, including the
# second-order effect on RRF, is in the design spec under "RLS and approximate
# indexes". Because there is no index there is also no new relation, so
# `EXPECTED_RELATIONS` in tests/test_demo_orgs.py is untouched by this task.
_VECTOR_SQL = text(
    """
    SELECT id, org_id, content, page_from, page_to,
           1 - (embedding OPERATOR(public.<=>) :qvec) AS score
      FROM document_chunks
     WHERE embedding IS NOT NULL
     ORDER BY embedding OPERATOR(public.<=>) :qvec
     LIMIT :limit
    """
).bindparams(bindparam("qvec", type_=Vector(DIMENSIONS)))


def _to_citations(rows) -> list[Citation]:
    """One shape for every mode, so a mode cannot invent a field or lose one."""
    return [
        {
            "chunk_id": str(row["id"]),
            "org_id": str(row["org_id"]),
            "content": row["content"][:400],
            "page_from": row["page_from"],
            "page_to": row["page_to"],
            # A real relevance score, and the value the rows were ordered by.
            # What it *means* depends on the mode and the field does not
            # pretend otherwise: `ts_rank` in `FTS` is unnormalised, so 0.07
            # means "better than the 0.03 below it" and means nothing next to a
            # 0.07 from a different question; cosine similarity in `VECTOR` is
            # bounded and comparable. Neither is comparable with the other,
            # which is why task 21 fuses ranks rather than scores.
            "score": float(row["score"]),
        }
        for row in rows
    ]


async def _search_fts(session: AsyncSession, query: str, limit: int) -> list[Citation]:
    rows = (
        await session.execute(
            _SEARCH_SQL,
            {
                "config": _TEXT_SEARCH_CONFIG,
                "question": query[:_MAX_QUERY_CHARS],
                "limit": limit,
            },
        )
    ).mappings().all()
    return _to_citations(rows)


async def _search_vector(session: AsyncSession, query: str, limit: int) -> list[Citation]:
    # `aencode_query`, never `encode_query`. The async variant is the one task 19
    # wrapped in a semaphore and pushed onto a thread; a synchronous inference
    # here runs on the event loop of an API started with `--workers 1` and pauses
    # every concurrent SSE stream for its whole duration — the exact bug the
    # streaming design exists to avoid.
    #
    # Truncated to the same bound as the lexical branch. It is the work the
    # question is allowed to cost, and it should not depend on which mode the
    # visitor picked.
    qvec = await get_embedder().aencode_query(query[:_MAX_QUERY_CHARS])
    rows = (
        await session.execute(_VECTOR_SQL, {"qvec": qvec, "limit": limit})
    ).mappings().all()
    return _to_citations(rows)


async def search_chunks(
    session: AsyncSession, query: str, *, mode: SearchMode, limit: int = 5
) -> list[Citation]:
    """Find the chunks most relevant to a question, best first.

    Two paths to the same rows. `FTS` lexes the question, ORs its lexemes into a
    `tsquery` and ranks by `ts_rank`; `VECTOR` embeds the question and ranks by
    cosine similarity against `document_chunks.embedding`. Task 21 adds `HYBRID`,
    which fuses the two by rank.

    `mode` is keyword-only and has no default. The name and the return type are
    unchanged from M1 — the docstring here used to promise that the call sites
    would not change either, and that promise is now retired: giving `mode` a
    default would let a call site that forgot to read the visitor's stored choice
    keep working in `FTS`, which is the inert switch this milestone removes.
    All twenty-four existing call sites pass it explicitly.

    There is deliberately no `WHERE org_id = ...` clause in either statement.
    Row-level security supplies it, and the tests hold that rather than assume
    it: `test_search_chunks_never_sees_another_tenants_chunk` and
    `test_vector_search_returns_only_the_callers_own_chunks` show another
    tenant's rows do not come back on either path, and
    `test_search_chunks_adds_no_org_id_filter_of_its_own` shows the statement
    that reaches Postgres never asked them not to — so what excludes them is the
    policy.
    """
    if mode is SearchMode.VECTOR:
        return await _search_vector(session, query, limit)
    return await _search_fts(session, query, limit)
