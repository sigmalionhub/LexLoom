import uuid

from sqlalchemy import text

from app.chat.retrieval import search_chunks
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


async def test_a_bare_wildcard_matches_nothing_rather_than_the_whole_corpus(
    created_orgs: list[str],
) -> None:
    """`%` is the question, not the pattern.

    Unescaped it compiles to `ILIKE '%%%'`, which matches every chunk the tenant
    owns. Not a leak — RLS still holds the boundary — but the endpoint would
    cite the entire corpus as the source of an answer it had nothing to do with.
    """
    org = await _new_org(created_orgs)
    await _seed_chunk(org, _CLAUSE)
    async with tenant_session(org) as session:
        assert await search_chunks(session, "%") == []


async def test_an_underscore_does_not_stand_in_for_a_character(
    created_orgs: list[str],
) -> None:
    """`_` is ILIKE's single-character wildcard, so "notify_the" would silently
    match "notify the" — a quieter version of the same falsehood."""
    org = await _new_org(created_orgs)
    await _seed_chunk(org, _CLAUSE)
    async with tenant_session(org) as session:
        assert await search_chunks(session, "notify_the") == []
        # The control: the same phrase written literally does match, so the
        # test above is about the wildcard and not about the seeding.
        assert len(await search_chunks(session, "notify the")) == 1


async def test_a_trailing_backslash_does_not_escape_the_wildcards_this_query_adds(
    created_orgs: list[str],
) -> None:
    """The escape character has to be escaped too.

    Left alone, a question ending in a backslash escapes the `%` this function
    appends, and the pattern stops meaning "contains" — it starts hunting for a
    literal per-cent sign that is not there.
    """
    org = await _new_org(created_orgs)
    await _seed_chunk(org, "See C:\\temp for the export bundle.")
    async with tenant_session(org) as session:
        assert len(await search_chunks(session, "C:\\")) == 1


async def test_a_question_longer_than_the_limit_is_truncated_before_it_is_escaped(
    created_orgs: list[str],
) -> None:
    """Truncating after escaping would cut a `\\%` pair in half.

    The severed backslash then escapes the `%` this function appends, so the
    pattern stops ending in a wildcard and starts demanding that the chunk end
    exactly where the question was cut. The chunk below continues past that
    point, so it is found in the right order and missed in the wrong one.
    """
    org = await _new_org(created_orgs)
    # 199 characters and then a per-cent sign: exactly the 200-character limit,
    # so the escape pair straddles it once escaping runs first.
    question = "a" * 199 + "% and the clause continues well past the cut."
    await _seed_chunk(org, f"{question} It really does.")
    async with tenant_session(org) as session:
        assert len(await search_chunks(session, question)) == 1


async def test_a_literal_percent_in_the_question_finds_the_chunk_that_has_one(
    created_orgs: list[str],
) -> None:
    org = await _new_org(created_orgs)
    await _seed_chunk(org, "The service credit is 5% of the monthly fee.")
    async with tenant_session(org) as session:
        hits = await search_chunks(session, "is 5%")
        assert len(hits) == 1
        assert hits[0]["page_from"] == 1


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
        hits = await search_chunks(session, "obligations under clause 4")

    assert [hit["org_id"] for hit in hits] == [mine]
