import re
import uuid

import pytest
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

    The rule applied: `org_id` may appear where columns are *chosen*, and
    nowhere that rows are *restricted*. So every `SELECT … FROM` span is
    removed — the citation payload returns `org_id`, and the query has more
    than one select list since retrieval moved to a CTE — and the assertion
    runs on what is left, which is the FROM, WHERE, ORDER BY and LIMIT clauses
    of every level of the statement. A filter tucked inside a subquery in the
    FROM clause survives the stripping too, because its own `SELECT … FROM` is
    removed separately and its WHERE is not.
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
    restricting_clauses = re.sub(
        r"\bSELECT\b.*?\bFROM\b", " ", reads[0], flags=re.DOTALL | re.IGNORECASE
    )
    assert "org_id" in reads[0], (
        "org_id is no longer in the statement at all, so the stripping below would "
        "pass without testing anything"
    )
    assert "org_id" not in restricting_clauses, (
        "search_chunks now restricts by org_id itself, so the test above no longer "
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

    A permissive extra policy on `document_chunks` naming the public corpus
    organisation would serve every tenant the same rows for free — and would put
    a permanent hole in the exact property this repository exists to
    demonstrate, in the one table a visitor actually reads.

    What makes this test the one that refuses it is its first line. Two of the
    invariant tests in `test_isolation_invariants.py` do also fail against such
    a policy — but only because *this* database happens to have a populated
    public corpus organisation for them to see. They read whatever rows exist;
    on a deployment where `scripts/seed_corpus.py` was never run, the corpus
    organisation is empty, the backdoor returns nothing to notice, and the whole
    invariant module passes with it installed. This test seeds the corpus as its
    own precondition, so what it asserts does not depend on what the database
    happened to be holding.
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


def test_every_way_the_corpus_file_can_fail_names_the_file(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One error type for all of them, and it says which path it read.

    The corpus is located by walking up from `app/chat/corpus.py`, so "which
    file did it actually look at" is the first question anyone debugging this
    will have — and the error exists to answer it. Catching only `OSError`
    covered the missing file and let malformed JSON escape as a bare
    `JSONDecodeError` and a renamed key as a bare `KeyError`: half the failure
    modes walking past the error written to explain them.

    The cache is cleared on both sides. `load_excerpts` memoises, so a corpus
    read here would otherwise be served to the tests that follow.
    """
    import app.chat.corpus as corpus_module

    broken = {
        "missing file": None,
        "not JSON at all": "{",
        "no excerpts key": '{"source": {}}',
        "excerpts is not a list of objects": '{"excerpts": ["just a string"]}',
        "an excerpt is missing a field": '{"excerpts": [{"content": "x", "page_from": 1}]}',
        "a page number is not a number": (
            '{"excerpts": [{"content": "x", "page_from": "one", "page_to": 1}]}'
        ),
        "no excerpts at all": '{"excerpts": []}',
    }

    corpus_module.load_excerpts.cache_clear()
    try:
        for description, body in broken.items():
            path = tmp_path / f"{abs(hash(description))}.json"
            if body is not None:
                path.write_text(body, encoding="utf-8")
            monkeypatch.setattr(corpus_module, "SEED_FILE", path)
            corpus_module.load_excerpts.cache_clear()

            with pytest.raises(corpus_module.CorpusUnavailableError) as raised:
                corpus_module.load_excerpts()
            assert str(path) in str(raised.value), (
                f"the error for {description!r} does not name the file it read"
            )
    finally:
        monkeypatch.undo()
        corpus_module.load_excerpts.cache_clear()

    # The real file still loads, so nothing above left a broken path or a
    # poisoned cache behind for the tests that follow.
    assert len(load_excerpts()) == 30


async def test_seeding_the_public_corpus_twice_leaves_one_copy() -> None:
    """`seed_corpus.py` is documented as idempotent, so re-running it is checked.

    The organisation it creates has no expiry and is therefore never purged: a
    seeder that appended would grow the corpus by a full copy on every
    deployment, and the duplicates would surface as repeated citation cards
    rather than as an error.

    This test permanently creates organisation `000…001` in whatever database it
    runs against, which is deliberate and bounded: the seeder is idempotent, so
    a database gains one organisation and one corpus the first time the suite
    runs and nothing on any run after that. Measured across two consecutive full
    runs, the organisation and chunk counts do not move. It is not registered
    for teardown because the row is meant to outlive the run — that is what
    `expires_at IS NULL` says.
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


async def test_the_seeded_rows_are_the_file_they_came_from() -> None:
    """Counting rows would let the seeder write thirty of the wrong thing.

    What this cannot check, and does not claim to: whether a *deployment's*
    seed organisation is current. These rows are a copy taken whenever the
    seeder last ran, so a database seeded from an older file keeps the older
    text, and no test that seeds first — this one included — can observe that.
    Nothing in the running application reads these rows today (`copy_corpus_into`
    reads the file), so the drift is invisible until M2 computes embeddings over
    them. What is checked here is the half that can be: a run leaves the rows
    equal to the file, field for field.
    """
    await seed_public_corpus()

    async with tenant_session(str(PUBLIC_CORPUS_ORG_ID)) as session:
        rows = (
            await session.execute(
                text("SELECT content, page_from, page_to FROM document_chunks")
            )
        ).all()

    assert sorted(rows) == sorted(
        (excerpt.content, excerpt.page_from, excerpt.page_to) for excerpt in load_excerpts()
    ), "the seeded rows are not the excerpts in gdpr_excerpts.json"
