from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

Citation = dict[str, object]

# ILIKE reads `%`, `_` and the escape character itself as pattern syntax. A
# question is not a pattern: `%` alone would match every chunk the tenant owns
# and `_` would quietly widen every other query by one character. Escaping them
# is what makes the docstring's "literal match" true rather than aspirational.
# The mapping must include the escape character, otherwise a trailing backslash
# in the question escapes the `%` this code appends and the pattern stops
# meaning what it says.
_LIKE_METACHARACTERS = str.maketrans({"\\": "\\\\", "%": "\\%", "_": "\\_"})

# Long questions are truncated rather than rejected: the point of the M1 match
# is to find *something* to cite, and a substring search on 200 characters of
# prose already finds nothing. Truncating before escaping would be a bug — it
# could cut a `\%` pair in half — so the order below is deliberate.
_MAX_QUERY_CHARS = 200


async def search_chunks(session: AsyncSession, query: str, limit: int = 5) -> list[Citation]:
    """Find chunks relevant to a question by literal, case-insensitive substring.

    M1 uses a literal match; M2 replaces the body with hybrid vector + full-text
    search fused by RRF. The signature and the call sites do not change.

    There is deliberately no `WHERE org_id = ...` clause here. Row-level
    security supplies it, and `test_search_chunks_never_sees_another_tenants_chunk`
    asserts that against this function — proving the policy works rather than
    proving a filter was remembered.
    """
    pattern = f"%{query[:_MAX_QUERY_CHARS].translate(_LIKE_METACHARACTERS)}%"
    rows = (
        await session.execute(
            text(
                r"""
                SELECT id, org_id, content, page_from, page_to
                FROM document_chunks
                WHERE content ILIKE :pattern ESCAPE '\'
                ORDER BY page_from
                LIMIT :limit
                """
            ),
            {"pattern": pattern, "limit": limit},
        )
    ).mappings().all()

    return [
        {
            "chunk_id": str(row["id"]),
            "org_id": str(row["org_id"]),
            "content": row["content"][:400],
            "page_from": row["page_from"],
            "page_to": row["page_to"],
            # Every hit is equally good under a substring match. The field
            # exists so the SSE payload keeps its shape when M2 starts
            # returning a real fused rank, not because this is a score.
            "score": 1.0,
        }
        for row in rows
    ]
