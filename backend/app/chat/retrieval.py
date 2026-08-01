from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

Citation = dict[str, object]

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


async def search_chunks(session: AsyncSession, query: str, limit: int = 5) -> list[Citation]:
    """Find the chunks most relevant to a question, best first.

    Postgres full-text search: the question is lexed, its lexemes are OR-ed into
    a `tsquery`, and the hits come back ordered by `ts_rank`. M2 replaces the
    body with hybrid vector + full-text search fused by RRF; the signature and
    the call sites do not change.

    There is deliberately no `WHERE org_id = ...` clause here. Row-level
    security supplies it, and two tests hold that rather than assume it:
    `test_search_chunks_never_sees_another_tenants_chunk` shows another tenant's
    rows do not come back, and `test_search_chunks_adds_no_org_id_filter_of_its_own`
    shows the statement that reaches Postgres never asked them not to — so what
    excludes them is the policy.
    """
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

    return [
        {
            "chunk_id": str(row["id"]),
            "org_id": str(row["org_id"]),
            "content": row["content"][:400],
            "page_from": row["page_from"],
            "page_to": row["page_to"],
            # A real relevance score now, and the value the rows were ordered
            # by. It is comparable within one response and nowhere else:
            # `ts_rank` is not normalised, so 0.07 here means "better than the
            # 0.03 below it" and means nothing next to a 0.07 from a different
            # question. M2 replaces it with a fused rank and the field keeps
            # its shape.
            "score": float(row["score"]),
        }
        for row in rows
    ]
