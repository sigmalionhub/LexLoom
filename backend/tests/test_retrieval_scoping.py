import uuid

from httpx import AsyncClient
from sqlalchemy import event, text

from app.chat.corpus import PUBLIC_CORPUS_ORG_ID, load_excerpts, seed_public_corpus
from app.chat.retrieval import search_chunks
from app.db.engine import engine
from app.db.session import system_session, tenant_session


async def test_search_returns_only_the_caller_org_chunks(two_orgs) -> None:
    """Org A asks a question both tenants' chunks answer and is shown only its own.

    What this asserts is the outcome, not the mechanism: the same result would
    follow from an explicit `WHERE org_id = ...` inside `search_chunks`. The
    mechanism is pinned separately —
    `test_search_chunks_adds_no_org_id_filter_of_its_own` below shows the
    statement carries no such clause, and the catalog-driven policy tests in
    `test_isolation_invariants.py` show the policy that does the cutting is
    enabled, forced, and free of an IS NULL bypass. The three together are what
    make "row-level security supplies the boundary" a checked claim.
    """
    org_a, org_b = two_orgs
    async with tenant_session(org_a) as session:
        citations = await search_chunks(session, "secret", limit=10)

    assert citations, "org A must see its own chunk"
    assert {citation["org_id"] for citation in citations} == {org_a}
    assert org_b not in {citation["org_id"] for citation in citations}


async def test_search_chunks_adds_no_org_id_filter_of_its_own(two_orgs) -> None:
    """The statement that reaches Postgres restricts nothing by organisation.

    `search_chunks` documents that it deliberately omits `WHERE org_id = ...`
    because the policy supplies it. That is a claim about the SQL, so it is
    checked against the SQL actually sent rather than against the source text —
    a filter added through a bound parameter, a helper, or a wrapper would still
    show up here.

    Only the part after `FROM` is inspected: `org_id` is in the select list on
    purpose, because the caller returns it in the citation payload.
    """
    org_a, _ = two_orgs
    issued: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        issued.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        async with tenant_session(org_a) as session:
            await search_chunks(session, "secret", limit=10)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)

    reads = [statement for statement in issued if "document_chunks" in statement]
    assert len(reads) == 1, f"expected one read of document_chunks, saw {len(reads)}"
    after_from = reads[0].split("FROM", 1)[1]
    assert "org_id" not in after_from, (
        "search_chunks now filters by org_id itself, so the test above no longer "
        "demonstrates that the row-level security policy is what confines the search:\n"
        f"{reads[0]}"
    )


async def test_search_finds_nothing_for_an_org_with_no_chunks(created_orgs: list[str]) -> None:
    """A tenant that owns nothing is shown nothing rather than everybody's rows.

    The organisation is registered for teardown: the development database is
    shared between runs, and a test that creates a tenant and walks away is how
    it came to hold well over a thousand of them.
    """
    empty_org = str(uuid.uuid4())
    async with system_session() as session:
        await session.execute(
            text("INSERT INTO organizations (id, external_id, name) VALUES (:i, :e, 'Empty')"),
            {"i": empty_org, "e": f"ext_{empty_org}"},
        )
    created_orgs.append(empty_org)

    async with tenant_session(empty_org) as session:
        assert await search_chunks(session, "secret", limit=10) == []


async def _demo_org(client: AsyncClient, created_orgs: list[str], label: str = "Acme") -> str:
    body = (await client.post("/api/v1/auth/demo-login", json={"label": label})).json()
    created_orgs.append(body["org_id"])
    return body["org_id"]


async def test_a_demo_tenant_has_a_corpus_before_it_has_uploaded_anything(
    client: AsyncClient, created_orgs: list[str]
) -> None:
    """The demo is empty without this, and empty in the way nobody notices.

    A visitor gets a brand-new organisation and asks the first question. With no
    chunks of their own, `search_chunks` returns `[]`, the `citations` event
    carries nothing, and the one thing the demo exists to show — sources
    rendering before the first token — silently does not happen. Nothing errors,
    so nothing reports it.
    """
    org_id = await _demo_org(client, created_orgs)

    async with tenant_session(org_id) as session:
        citations = await search_chunks(session, "processor", limit=10)

    assert citations, "a fresh demo tenant has nothing to cite, so its first answer has no sources"
    assert {citation["org_id"] for citation in citations} == {org_id}

    async with tenant_session(org_id) as session:
        owned = await session.scalar(text("SELECT count(*) FROM document_chunks"))
    assert owned == len(load_excerpts()), "the demo tenant did not receive the whole corpus"


async def test_two_demo_tenants_share_no_chunk(
    client: AsyncClient, created_orgs: list[str]
) -> None:
    """Each visitor owns their own copy, which is what keeps the thesis honest.

    Serving one shared set of rows to every tenant would be cheaper and would
    make this project's headline claim — retrieval is cut by organisation —
    false for the only corpus the demo actually has. So the chunks are copied,
    and the row identities have to be disjoint.
    """
    first = await _demo_org(client, created_orgs, "Acme")
    second = await _demo_org(client, created_orgs, "Globex")

    async def chunk_ids(org_id: str) -> set[str]:
        async with tenant_session(org_id) as session:
            rows = (await session.execute(text("SELECT id FROM document_chunks"))).scalars()
            return {str(row) for row in rows}

    mine, theirs = await chunk_ids(first), await chunk_ids(second)
    assert mine and theirs
    assert len(mine) == len(theirs) == len(load_excerpts())
    assert mine.isdisjoint(theirs), "two demo tenants are reading the same rows"


async def test_the_public_corpus_organisation_is_invisible_to_a_demo_tenant(
    client: AsyncClient, created_orgs: list[str]
) -> None:
    """The corpus is copied, never shared through a policy exception.

    A permissive `USING (org_id = <the seed organisation> OR ...)` on
    `document_chunks` would serve every tenant the same rows for free — and
    would put a permanent hole in the exact property this repository exists to
    demonstrate, in the one table a visitor actually reads. That change would
    leave every other test in this suite green. This is the one it fails.
    """
    await seed_public_corpus()
    org_id = await _demo_org(client, created_orgs)

    async with tenant_session(org_id) as session:
        visible = await session.scalar(
            text("SELECT count(*) FROM document_chunks WHERE org_id = :o"),
            {"o": str(PUBLIC_CORPUS_ORG_ID)},
        )
        citations = await search_chunks(session, "processor", limit=50)

    assert visible == 0, "a demo tenant can read the public corpus organisation's own rows"
    assert str(PUBLIC_CORPUS_ORG_ID) not in {citation["org_id"] for citation in citations}


async def test_seeding_the_public_corpus_twice_leaves_one_copy() -> None:
    """`seed_corpus.py` is documented as idempotent, so re-running it is checked.

    The organisation it creates has no expiry and is therefore never purged: a
    seeder that appended would grow the corpus by a full copy on every
    deployment, and the duplicates would surface as repeated citation cards
    rather than as an error.
    """
    assert await seed_public_corpus() == len(load_excerpts())
    assert await seed_public_corpus() == len(load_excerpts())

    async with tenant_session(str(PUBLIC_CORPUS_ORG_ID)) as session:
        stored = await session.scalar(text("SELECT count(*) FROM document_chunks"))
        expires_at = await session.scalar(
            text("SELECT expires_at FROM organizations WHERE id = :o"),
            {"o": str(PUBLIC_CORPUS_ORG_ID)},
        )
    assert stored == len(load_excerpts())
    assert expires_at is None, "the public corpus organisation would be purged after 24 hours"
