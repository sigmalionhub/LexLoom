from app.chat.prompt import build_grounded_prompt
from app.ingest.catalogue import CATALOGUE


class _RecordingProvider:
    """Stands in for `FakeProvider` in the stream endpoint and remembers the
    prompt it was actually handed, without changing the SSE shape the client
    sees — it still yields a couple of tokens so the stream completes.

    A class-level list rather than an instance attribute: `conversations.py`
    constructs the provider itself (`FakeProvider()`), so the test never holds
    a reference to the instance that receives the call.
    """

    received_prompts: list[str] = []

    def __init__(self, delay: float = 0.0) -> None:
        del delay

    async def stream(self, prompt: str):
        _RecordingProvider.received_prompts.append(prompt)
        yield "ok "


_CITATIONS = [
    {
        "chunk_id": "11111111-1111-1111-1111-111111111111",
        "org_id": "org-a",
        "content": "The processor shall notify the controller within 72 hours.",
        "page_from": 52,
        "page_to": 52,
        "score": 0.03,
    },
    {
        "chunk_id": "22222222-2222-2222-2222-222222222222",
        "org_id": "org-a",
        "content": "The processor must implement appropriate technical measures.",
        "page_from": 12,
        "page_to": 13,
        "score": 0.01,
    },
]


def test_the_prompt_carries_the_source_text() -> None:
    """This is the whole point of the module: a real provider only ever sees
    text that came from this tenant's own retrieved chunks, never the raw
    question routed straight to a shared API key with no grounding at all."""
    prompt = build_grounded_prompt("What must be done within 72 hours?", _CITATIONS)
    assert _CITATIONS[0]["content"] in prompt
    assert _CITATIONS[1]["content"] in prompt


def test_the_prompt_carries_the_question() -> None:
    prompt = build_grounded_prompt("What must be done within 72 hours?", _CITATIONS)
    assert "What must be done within 72 hours?" in prompt


def test_the_prompt_carries_all_three_instruction_clauses() -> None:
    """Saboteur check: delete the "say so" clause and this test — not a human
    skim of the prompt string — is what turns red."""
    prompt = build_grounded_prompt("q", _CITATIONS).lower()
    # 1. Answer only from the sources.
    assert "answer strictly from the numbered sources" in prompt
    # 2. Cite them, numbered to match the citations list.
    assert "cite it inline" in prompt or "in brackets" in prompt
    # 3. The clause that matters: an unanswerable question must say so, not
    #    invent a plausible-looking citation.
    assert "if the sources do not contain the answer, say so" in prompt


def test_sources_are_numbered_in_citation_list_order() -> None:
    """The panel highlights fragment *n* when the answer says `[n]` — that
    mapping is positional, so renumbering the sources silently breaks it."""
    prompt = build_grounded_prompt("q", _CITATIONS)
    first_index = prompt.index(_CITATIONS[0]["content"])
    second_index = prompt.index(_CITATIONS[1]["content"])
    assert prompt.rindex("[1]", 0, first_index) != -1
    assert prompt.rindex("[2]", 0, second_index) != -1
    assert first_index < second_index


def test_no_citations_still_produces_a_usable_prompt() -> None:
    """A question with no retrieval hits must not crash prompt assembly — it
    has to reach the model so it can say, per clause three, that it found
    nothing."""
    prompt = build_grounded_prompt("an unanswerable question", [])
    assert "an unanswerable question" in prompt
    assert "no sources were found" in prompt.lower()


async def test_the_stream_endpoint_hands_the_provider_a_grounded_prompt_not_the_raw_question(
    client, demo_auth, demo_org_id, monkeypatch
) -> None:
    """The whole point of Step 2's reorder, proven against the real endpoint
    rather than the pure function.

    Saboteur check: revert `conversations.py` to `prompt = message["content"]`
    and this goes red — the recorded prompt would be exactly the six-word
    question, with none of the retrieved source text and none of the three
    instruction clauses.

    The retrieved text is not a literal any more. It used to assert
    `Article 33(1)`, a phrase from the seeded excerpts' own typography — and
    those excerpts stopped being the whole of a tenant's corpus when
    provisioning started handing out the full regulation, whose chunks carry the
    Official Journal's wording instead. Rather than swap one literal for
    another, the test now runs the same retrieval the endpoint ran and requires
    that what came back is in the prompt. That is the claim the literal was
    standing in for, and it survives the next change of corpus.
    """
    _RecordingProvider.received_prompts.clear()
    # `conversations.py` now builds its provider through `build_provider`
    # (Task 29), not by constructing `FakeProvider` directly — patch that
    # factory instead, ignoring the `GeneratorChoice` it is called with, since
    # this test is about prompt assembly, not selection.
    monkeypatch.setattr(
        "app.api.v1.conversations.build_provider", lambda _choice: _RecordingProvider()
    )

    conversation_id = (
        await client.post("/api/v1/conversations", json={"title": "t"}, headers=demo_auth)
    ).json()["id"]
    question = "What must be done within 72 hours?"
    message_id = (
        await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"content": question},
            headers=demo_auth,
        )
    ).json()["message_id"]
    ticket = (
        await client.post(
            "/api/v1/auth/stream-tickets", json={"message_id": message_id}, headers=demo_auth
        )
    ).json()["ticket"]

    url = f"/api/v1/conversations/{conversation_id}/messages/{message_id}/stream?ticket={ticket}"
    async with client.stream("GET", url) as response:
        async for _line in response.aiter_lines():
            pass

    assert len(_RecordingProvider.received_prompts) == 1
    prompt = _RecordingProvider.received_prompts[0]
    assert question in prompt

    # Proof the provider received more than the bare question: what retrieval
    # actually returned for this tenant is in there.
    from app.chat.retrieval import SearchMode, search_chunks
    from app.db.session import tenant_session

    async with tenant_session(demo_org_id) as session:
        cited = await search_chunks(session, question, mode=SearchMode.FTS, limit=5)
    assert cited, "retrieval returned nothing, so the assertion below proves nothing"
    assert any(str(chunk["content"]) in prompt for chunk in cited)
    assert "if the sources do not contain the answer, say so" in prompt.lower()


# --- A second document: where a chunk is, when its format has no pages -------

_NIST_KEY = "nist-sp-800-63b"
_NIST_PATH = "Authentication Assurance Levels › Authentication Assurance Level 2 › Reauthentication"


def _pageless_citation() -> dict:
    """A `.docx` chunk as `_to_citations` builds one: no pages, a locator."""
    return {
        "chunk_id": "00000000-0000-0000-0000-000000000001",
        "org_id": "00000000-0000-0000-0000-000000000002",
        "content": "Periodic reauthentication of subscriber sessions SHALL be performed.",
        "page_from": None,
        "page_to": None,
        "source_key": _NIST_KEY,
        "locator": _NIST_PATH,
        "score": 0.5,
    }


def test_a_page_less_chunk_is_never_rendered_as_page_none() -> None:
    """`None == None` is true, so the equality branch that produced "p. 12"
    produced "p. None" for every `.docx` chunk and handed it to the model as a
    position. The bug is one character of precedence and zero characters of
    output that look wrong to a human skimming a prompt."""
    rendered = build_grounded_prompt("when must a session reauthenticate?", [_pageless_citation()])
    assert "None" not in rendered
    assert _NIST_PATH in rendered


def test_a_citation_names_the_document_it_came_from() -> None:
    """The owner's third decision: retrieval searches the whole tenant corpus,
    so what makes an answer checkable is that each citation says which document
    it is from."""
    rendered = build_grounded_prompt("q", [_pageless_citation()])
    assert f"[1] ({_NIST_KEY}," in rendered


def test_document_titles_appear_once_rather_than_in_every_citation() -> None:
    """The GDPR's catalogue title is 280 characters and `search_chunks` runs at
    a default limit of 5, so naming the document inline would put ~1,400
    characters of identical text in one prompt. Asserted as a count, because
    "it's shorter" is not a property a test can hold."""
    citations = [_pageless_citation() for _ in range(5)]
    rendered = build_grounded_prompt("q", citations)
    assert rendered.count(CATALOGUE[_NIST_KEY].title) == 1
    assert rendered.count(f"({_NIST_KEY},") == 5
