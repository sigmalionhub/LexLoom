from app.chat.prompt import build_grounded_prompt


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
    client, demo_auth, monkeypatch
) -> None:
    """The whole point of Step 2's reorder, proven against the real endpoint
    rather than the pure function.

    Saboteur check: revert `conversations.py` to `prompt = message["content"]`
    and this goes red — the recorded prompt would be exactly the six-word
    question, with none of the retrieved source text (`Article 33(1)`, present
    in the seeded corpus and not in the question itself) and none of the three
    instruction clauses.
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
    # Source text that is in the seeded corpus and nowhere in the question —
    # proof the provider received more than the bare question.
    assert "Article 33(1)" in prompt
    assert "if the sources do not contain the answer, say so" in prompt.lower()
