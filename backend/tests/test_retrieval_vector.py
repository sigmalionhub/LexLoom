"""Vector retrieval: the second path to the same rows, held to the same boundary.

`search_chunks(mode=SearchMode.VECTOR)` reaches `document_chunks` through cosine
distance rather than through `tsquery`. Row-level security is supposed to cover
it automatically, because the policy is on the table and not on the statement —
but "supposed to" is exactly the kind of claim this repository exists to check
rather than assert.

The tests that need the real ONNX weights carry `@pytest.mark.model`; conftest
skips them when the cache is cold rather than downloading 90 MB on a bare CI
runner. The seed-vector contract below deliberately needs no model, so it runs
everywhere and is the half that fails the moment the corpus file is edited
without regenerating the vectors beside it.
"""

import json
import shutil

import pytest
from sqlalchemy import text

import app.chat.corpus as corpus_module
from app.chat.corpus import copy_corpus_into, load_excerpts, load_seed_vectors
from app.chat.embedding import DIMENSIONS, MODEL_NAME
from app.chat.retrieval import SearchMode, search_chunks
from app.db.session import tenant_session


async def _mark_chunks(session, marker: str) -> None:
    """Stamp every chunk this session can see with the name of its owner.

    Applied after the corpus is copied, and it touches `content` rather than
    `embedding` on purpose: it identifies the owner of a row without changing
    what the vector search actually ranks on, so both tenants stay equally
    findable by the same question.
    """
    await session.execute(
        text("UPDATE document_chunks SET content = :marker || ' ' || content"),
        {"marker": marker},
    )


@pytest.mark.model
async def test_vector_search_returns_only_the_callers_own_chunks(two_orgs) -> None:
    """Both tenants hold embedded chunks, with content that identifies its owner.

    Seeding only one tenant and asserting the other sees zero rows would prove
    nothing at all. With no `app.current_org_id` set, the policy predicate
    `org_id = nullif(current_setting('app.current_org_id', true), '')::uuid`
    evaluates to NULL, every row is filtered and the query returns zero rows with
    no error — so `hits == []` is satisfied by a *broken* isolation mechanism
    exactly as happily as by a working one.

    Seeding both sides means the failure mode is a WRONG row rather than NO row,
    and only a real leak can produce it. That is what `assert hits` on the first
    line is for: a test that asserts only absence is content with a mechanism
    that returns nothing for the wrong reason.

    No embedding step here: `copy_corpus_into` writes the vector with the row,
    from `seed/gdpr_excerpts_vectors.json`. The question still has to be embedded,
    which is why this test is marked `model`.
    """
    a, b = two_orgs
    async with tenant_session(a) as session:
        await copy_corpus_into(session, a)
        await _mark_chunks(session, marker="OWNED-BY-A")
    async with tenant_session(b) as session:
        await copy_corpus_into(session, b)
        await _mark_chunks(session, marker="OWNED-BY-B")
        hits = await search_chunks(session, "processor obligations", mode=SearchMode.VECTOR)

    assert hits, "tenant b must see its own chunks — an empty result proves nothing"
    # str() on both sides: the projection returns whatever the driver gives for a
    # uuid column, and `two_orgs` yields whatever the fixture built. Comparing the
    # two without normalising is how this assertion passes vacuously.
    assert all(str(hit["org_id"]) == str(b) for hit in hits)
    assert all("OWNED-BY-A" not in hit["content"] for hit in hits)


@pytest.mark.model
async def test_the_vector_statement_adds_no_org_id_filter_of_its_own(two_orgs) -> None:
    """The vector branch confines nothing by organisation; the policy does.

    The same rule the lexical branch is held to, applied to the second path:
    `org_id` may appear where columns are *chosen* and nowhere that rows are
    *restricted*. Without this, the test above would still pass against a
    `search_chunks` that had quietly grown its own `WHERE org_id = ...` — and
    would then be evidence about that clause rather than about the policy.
    """
    import re

    from sqlalchemy import event

    from app.db.engine import engine

    org_a, _ = two_orgs
    issued: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        issued.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        async with tenant_session(org_a) as session:
            await search_chunks(session, "processor obligations", mode=SearchMode.VECTOR)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)

    reads = [statement for statement in issued if "document_chunks" in statement]
    assert len(reads) == 1, f"expected one read of document_chunks, saw {len(reads)}"
    assert "org_id" in reads[0], (
        "org_id is no longer in the statement at all, so the stripping below would "
        "pass without testing anything"
    )
    restricting_clauses = re.sub(
        r"\bSELECT\b.*?\bFROM\b", " ", reads[0], flags=re.DOTALL | re.IGNORECASE
    )
    assert "org_id" not in restricting_clauses, (
        "the vector branch now restricts by org_id itself, so the isolation test no "
        f"longer demonstrates that row-level security is what confines it:\n{reads[0]}"
    )


@pytest.mark.model
async def test_vector_search_answers_a_question_that_shares_no_word_with_the_chunk(
    client, created_orgs: list[str]
) -> None:
    """The property vector search is added for, stated so it cannot pass by accident.

    A question phrased in words the corpus does not use is what full-text search
    cannot answer: no shared lexeme means no `tsquery` match and an empty
    `citations` frame. The control is the point — the same question in the same
    tenant returns nothing under `FTS` and returns the right article under
    `VECTOR`.
    """
    body = (await client.post("/api/v1/auth/demo-login", json={"label": "Acme"})).json()
    created_orgs.append(body["org_id"])

    # "72 hours" and "supervisory authority" are the corpus's words for it;
    # none of these are.
    question = "How quickly does a company have to tell the regulator about a leak?"
    async with tenant_session(body["org_id"]) as session:
        lexical = await search_chunks(session, question, mode=SearchMode.FTS)
        semantic = await search_chunks(session, question, mode=SearchMode.VECTOR)

    assert semantic, "vector search found nothing for a question phrased in other words"
    assert all(0.0 <= hit["score"] <= 1.0 for hit in semantic), (
        f"cosine similarity out of range: {[hit['score'] for hit in semantic]}"
    )
    scores = [hit["score"] for hit in semantic]
    assert scores == sorted(scores, reverse=True), (
        "the reported scores are not the order the rows arrived in"
    )
    assert not any("72 hours" in hit["content"] for hit in lexical), (
        "the lexical control matched after all, so this test says nothing about "
        "what vector search adds — pick a question with fewer shared stems"
    )
    assert any("72 hours" in hit["content"] for hit in semantic), (
        "vector search did not surface the breach-notification article:\n"
        + "\n".join(str(hit["content"])[:120] for hit in semantic)
    )


async def test_an_unembedded_chunk_is_absent_from_a_vector_search(created_orgs) -> None:
    """A chunk with no vector must not be ranked, and must not be ranked *first*.

    `document_chunks.embedding` is nullable — a chunk ingested before its
    backfill ran has none — and `NULL <=> v` is NULL, which is not "far away".
    Whether a NULL sorts to the top or the bottom of an `ORDER BY` is a property
    of the sort direction, not of the data, so leaving those rows in the scan
    means an unembedded chunk can be cited as the best match for every question.
    The `WHERE embedding IS NOT NULL` clause is what prevents it, and this is the
    test that notices if it goes away.

    No model needed: the seed vectors come off disk and the query vector is a
    literal, because what is under test is the SQL rather than the embedding.
    """
    import uuid

    from app.db.session import system_session

    org_id = str(uuid.uuid4())
    async with system_session() as session:
        await session.execute(
            text("INSERT INTO organizations (id, external_id, name) VALUES (:i, :e, 'Vec')"),
            {"i": org_id, "e": f"ext_{org_id}"},
        )
    created_orgs.append(org_id)

    async with tenant_session(org_id) as session:
        await copy_corpus_into(session, org_id)
        await session.execute(
            text(
                "INSERT INTO document_chunks (id, org_id, content, page_from, page_to) "
                "VALUES (:i, :o, 'NEVER EMBEDDED', 99, 99)"
            ),
            {"i": str(uuid.uuid4()), "o": org_id},
        )

    from app.chat.retrieval import _VECTOR_SQL

    async with tenant_session(org_id) as session:
        rows = (
            await session.execute(
                _VECTOR_SQL, {"qvec": list(load_seed_vectors()[0]), "limit": 50}
            )
        ).mappings().all()

    assert rows, "the embedded chunks are missing, so this proves nothing about the null one"
    assert not any(row["content"] == "NEVER EMBEDDED" for row in rows)
    assert len(rows) == len(load_excerpts())


async def _embedded_count(org_id: str) -> int:
    async with tenant_session(org_id) as session:
        return await session.scalar(
            text("SELECT count(*) FROM document_chunks WHERE embedding IS NOT NULL")
        )


@pytest.mark.model
async def test_the_backfill_embeds_one_organisation_and_leaves_the_other_alone(
    two_orgs,
) -> None:
    """`lexloom.embed_org_corpus` is scoped by the policy, not by a WHERE clause.

    `two_orgs` gives each tenant one chunk written the old way, with no vector —
    which is exactly the state this task exists for: a chunk that arrived before
    anything embedded it. Running the backfill for A must fill A's row and leave
    B's alone, and B's row being *present and still NULL* is what makes that a
    statement about scoping rather than about the update having run at all.

    Invoked the way the worker does, off the test's event loop: the task calls
    `asyncio.run`, which refuses to start inside a loop that is already running.
    Same helper shape as `_run_purge` in test_demo_orgs.py, including disposing
    the engine on this side first — its pool is a module global, and a
    connection opened on this loop and used from the thread's loop fails in ways
    that point at the wrong code.
    """
    import asyncio

    from app.db.engine import engine
    from app.worker.tasks import embed_org_corpus

    a, b = two_orgs
    assert await _embedded_count(a) == 0 and await _embedded_count(b) == 0

    await engine.dispose()
    written = await asyncio.to_thread(lambda: embed_org_corpus.apply(args=[a]).get())

    assert written == 1
    assert await _embedded_count(a) == 1
    assert await _embedded_count(b) == 0, "the backfill reached another tenant's chunk"

    # Idempotent by construction rather than by a flag: the rows it wrote no
    # longer match `embedding IS NULL`, so a second run has nothing to do. A
    # backfill that re-embedded everything on every run would be a CPU bill that
    # grows with the corpus and never with the work.
    await engine.dispose()
    again = await asyncio.to_thread(lambda: embed_org_corpus.apply(args=[a]).get())
    assert again == 0


def test_the_seed_vectors_describe_the_corpus_that_is_actually_shipped() -> None:
    """The hash guard, exercised.

    This is the cheap half of the corpus contract: it needs no model, so CI runs
    it, and it fails the moment the corpus file is edited without regenerating
    the vectors beside it.
    """
    excerpts = load_excerpts()
    vectors = load_seed_vectors()
    assert len(vectors) == len(excerpts)
    assert all(len(vector) == DIMENSIONS for vector in vectors)


def test_editing_the_corpus_without_regenerating_the_vectors_is_refused(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One added space in the corpus, and the loader refuses — naming the file.

    This is the failure the hash exists for and it is otherwise silent: the
    vectors still load, still have the right count and the right width, and
    still describe the *previous* text. Retrieval keeps working and keeps being
    wrong, which is worse than not working.

    Both caches are cleared on both sides, and that is load-bearing rather than
    hygiene. `load_excerpts` and `load_seed_vectors` are both `@lru_cache`d, so
    after any earlier test in this process a substituted `SEED_FILE` changes
    nothing: the memoised value comes back, nothing raises, and the "proof"
    proves only that the cache works. `test_retrieval_scoping.py` says the same
    thing out loud about `load_excerpts` alone.

    Production has no such hole — the file is frozen into the image and nobody
    edits it under a live process. The cache is a problem for this test only,
    and only because the test deliberately does what production does not.
    """
    edited = tmp_path / "gdpr_excerpts.json"
    shutil.copy(corpus_module.SEED_FILE, edited)
    edited.write_text(edited.read_text(encoding="utf-8") + " ", encoding="utf-8")

    # The edit really is invisible to everything except the hash: same excerpts,
    # same count. Without this line the assertion below could be passing because
    # the copy is malformed.
    assert json.loads(edited.read_text(encoding="utf-8")) == json.loads(
        corpus_module.SEED_FILE.read_text(encoding="utf-8")
    )

    corpus_module.load_excerpts.cache_clear()
    corpus_module.load_seed_vectors.cache_clear()
    try:
        monkeypatch.setattr(corpus_module, "SEED_FILE", edited)
        assert len(corpus_module.load_excerpts()) == 30, (
            "the edited copy no longer parses, so the refusal below would be about "
            "the JSON rather than about the hash"
        )
        with pytest.raises(corpus_module.CorpusUnavailableError) as raised:
            corpus_module.load_seed_vectors()
        assert str(edited) in str(raised.value), (
            "the error does not name the corpus file it hashed"
        )
        assert str(corpus_module.SEED_VECTORS_FILE) in str(raised.value), (
            "the error does not name the vectors file that has to be regenerated"
        )
    finally:
        monkeypatch.undo()
        corpus_module.load_excerpts.cache_clear()
        corpus_module.load_seed_vectors.cache_clear()

    # The real pair still loads, so nothing above left a poisoned cache behind.
    assert len(load_seed_vectors()) == len(load_excerpts())


def test_the_seed_vectors_carry_a_vector_for_every_excerpt_in_file_order(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Count and width are checked at load, not trusted at insert.

    `copy_corpus_into` zips excerpts against vectors positionally — that
    position *is* the `(SEED_SOURCE_KEY, chunk_ordinal)` key the M2.4 gold set
    is built on — so a file carrying one vector too few would otherwise write a
    corpus one chunk short, and a demo tenant would be missing an article with
    nothing raised.
    """
    truncated = tmp_path / "gdpr_excerpts_vectors.json"
    document = json.loads(corpus_module.SEED_VECTORS_FILE.read_text(encoding="utf-8"))
    document["vectors"] = document["vectors"][:-1]
    truncated.write_text(json.dumps(document), encoding="utf-8")

    corpus_module.load_seed_vectors.cache_clear()
    try:
        monkeypatch.setattr(corpus_module, "SEED_VECTORS_FILE", truncated)
        with pytest.raises(corpus_module.CorpusUnavailableError) as raised:
            corpus_module.load_seed_vectors()
        assert str(truncated) in str(raised.value)
    finally:
        monkeypatch.undo()
        corpus_module.load_seed_vectors.cache_clear()


def test_changing_the_model_without_regenerating_the_vectors_is_refused(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Swapping `MODEL_NAME` alone passes every other check, so it gets its own.

    `corpus_sha256` ties the vectors to the *text*. Nothing ties them to the
    *weights*: change the model and the hash still matches, the count still
    matches, the width is still 384 — and the corpus lives in one vector space
    while the questions are embedded into another. Retrieval does not break, it
    degrades, and no guard is red.

    The recorded name is edited here rather than `MODEL_NAME`, which is the same
    comparison from the other side and leaves `app.chat.embedding` alone for the
    tests that run after this one.

    Deliberately *not* marked `model`: a string comparison needs no weights, so
    this runs on a cold CI cache — which is exactly the run that happens right
    after someone changed the model, and exactly when
    `test_the_committed_vectors_are_what_this_model_produces_today` is skipped.
    """
    other_model = tmp_path / "gdpr_excerpts_vectors.json"
    document = json.loads(corpus_module.SEED_VECTORS_FILE.read_text(encoding="utf-8"))
    assert document["model"] == MODEL_NAME, (
        "the committed vectors already disagree with MODEL_NAME — re-run "
        "scripts/embed_seed_corpus.py rather than editing this test"
    )
    document["model"] = "BAAI/bge-small-en-v1.5"
    other_model.write_text(json.dumps(document), encoding="utf-8")

    corpus_module.load_seed_vectors.cache_clear()
    try:
        monkeypatch.setattr(corpus_module, "SEED_VECTORS_FILE", other_model)
        with pytest.raises(corpus_module.CorpusUnavailableError) as raised:
            corpus_module.load_seed_vectors()
        message = str(raised.value)
        assert "BAAI/bge-small-en-v1.5" in message, "the error does not name the model it found"
        assert MODEL_NAME in message, "the error does not name the model this process uses"
        assert str(other_model) in message, "the error does not name the file to regenerate"
    finally:
        monkeypatch.undo()
        corpus_module.load_seed_vectors.cache_clear()


@pytest.mark.model
def test_the_committed_vectors_are_what_this_model_produces_today() -> None:
    """The half the hash cannot check: the vectors match the *weights* shipped.

    `corpus_sha256` ties the file to the corpus. Nothing ties it to the model —
    swap `MODEL_NAME` and the hash still matches, while every stored vector now
    describes the text through a different model than the one embedding the
    questions. Retrieval degrades to noise and no guard notices.

    Compared by cosine similarity rather than equality: the file stores six
    decimal places, so exact equality would fail on rounding alone.
    """
    from app.chat.embedding import get_embedder

    excerpts = load_excerpts()
    stored = load_seed_vectors()
    fresh = get_embedder().encode([excerpt.content for excerpt in excerpts])

    for index, (a, b) in enumerate(zip(stored, fresh, strict=True)):
        similarity = sum(x * y for x, y in zip(a, b, strict=True))
        assert similarity > 0.999, (
            f"vector {index} in {corpus_module.SEED_VECTORS_FILE.name} does not match what "
            f"the shipped model produces (cosine {similarity:.6f}) — re-run "
            "scripts/embed_seed_corpus.py"
        )
