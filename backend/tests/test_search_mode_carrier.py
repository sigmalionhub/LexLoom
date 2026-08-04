"""The mode chosen on POST is the mode the search actually runs in.

This is the carrier test, not a retrieval test: `test_retrieval_modes.py` shows
`HYBRID`/`VECTOR`/`FTS` are different searches. This file shows the choice a
visitor makes on `POST /messages` survives the round trip through
`messages.search_mode` and reaches `search_chunks` unchanged, and that the
frame reports the mode the answer was actually retrieved with.

Two variants of the same property, for a reason found in fix round 1 and worth
recording. `mode="vector"` looks like a request body value, not a model
dependency — but honouring it means `_search_vector` calls
`get_embedder().aencode_query(...)`, which downloads ~90 MB into an empty
cache exactly as eagerly as a test that calls the embedder directly. Measured:
running the POST-then-stream test below against an emptied
`FASTEMBED_CACHE_PATH` grew the directory from 0 to 88 MB. The trap is
transitive — this file never names the embedder — which is exactly how it got
past marking the direct model tests in the same commit.

Marking the whole test `model` closes that hole and opens a smaller one: the
carrier proof would then be skipped on every cold cache, which is precisely
the moment something about the model or the environment just changed and
needs checking most. So there are two: `..._runs_in` is the real end-to-end
path and stays `@pytest.mark.model`; `..._runs_in_without_the_real_model`
stubs the embedder — following the `stub_model` pattern in
`test_embedding.py`, patching the loaded `_model` rather than the loader, so a
model already cached by an earlier test in the same process cannot mask the
stub — and proves the identical property with zero bytes on disk.
"""

import json

import pytest
from sqlalchemy import text

import app.chat.embedding as embedding_module
from app.api.v1 import conversations
from app.chat.embedding import DIMENSIONS
from app.chat.retrieval import SearchMode
from app.db.session import system_session


async def _create_conversation(client, headers) -> str:
    response = await client.post("/api/v1/conversations", json={"title": "Demo"}, headers=headers)
    return response.json()["id"]


async def _collect_frames(client, headers, conversation_id: str, message_id: str) -> dict[str, str]:
    """Pair each `event:` line with the raw `data:` line that follows it.

    Kept private to this file rather than imported from `test_sse_contract.py`:
    that module owns a list-shaped `_events` helper for its own assertions, and
    this file wants the raw JSON string per event name instead.
    """
    ticket = (
        await client.post(
            "/api/v1/auth/stream-tickets", json={"message_id": message_id}, headers=headers
        )
    ).json()["ticket"]
    url = (
        f"/api/v1/conversations/{conversation_id}/messages/{message_id}/stream?ticket={ticket}"
    )
    frames: dict[str, str] = {}
    name = ""
    async with client.stream("GET", url) as response:
        async for line in response.aiter_lines():
            if line.startswith("event: "):
                name = line.removeprefix("event: ").strip()
            elif line.startswith("data: "):
                frames[name] = line.removeprefix("data: ")
    return frames


async def _post_vector_question_and_observe(
    client, headers: dict[str, str], monkeypatch
) -> tuple[list[SearchMode], dict[str, str]]:
    """POST a `mode="vector"` question, stream its answer, and report two things:
    the argument `search_chunks` was actually called with, and the raw SSE frames.

    Shared by both variants below so the mechanism under test — POST records a
    choice, GET reads it back, `search_chunks` sees it, the frame reports it —
    is written once. What differs between the two callers is only whether the
    real embedder answers `aencode_query` or a stub does.
    """
    seen: list[SearchMode] = []
    real = conversations.search_chunks

    async def recording(session, query, *, mode, limit=5):
        seen.append(mode)
        return await real(session, query, mode=mode, limit=limit)

    monkeypatch.setattr(conversations, "search_chunks", recording)

    conversation_id = await _create_conversation(client, headers)
    message_id = (
        await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"content": "What are the processor's obligations?", "mode": "vector"},
            headers=headers,
        )
    ).json()["message_id"]
    frames = await _collect_frames(client, headers, conversation_id, message_id)
    return seen, frames


@pytest.mark.model
async def test_the_mode_chosen_on_post_is_the_mode_the_search_runs_in(
    client, demo_auth, monkeypatch
) -> None:
    """What revision 1 shipped instead: a validated field that went nowhere.

    The choice is made on POST and consumed in a separate GET handler. Revision 1
    added the field to the request model, and there it stopped — no column, no
    claim in the ticket, nothing on the GET side. A plausible test ("POST with
    mode=vector returns 202") passed against a completely inert switch, which is
    why this one records the argument `search_chunks` was actually called with
    rather than the status code the POST returned.

    Marked `model`: `mode="vector"` reaches `_search_vector`, which calls
    `get_embedder().aencode_query(...)` on the question exactly as eagerly as a
    test that names the embedder directly — measured to pull ~90 MB into an
    empty `FASTEMBED_CACHE_PATH` (fix round 1). This is the real end-to-end
    path, kept for the property only real weights can show: that the actual
    embedder is reachable through the actual carrier. See
    `..._without_the_real_model` below for the same carrier property proved
    with no weights on disk, so a cold cache does not skip it entirely.
    """
    seen, frames = await _post_vector_question_and_observe(client, demo_auth, monkeypatch)

    assert seen == [SearchMode.VECTOR], "the stored choice never reached retrieval"
    # And the visitor can see it: the frame reports the mode the answer was
    # actually retrieved with, which is what makes a broken carrier visible
    # instead of merely wrong.
    assert json.loads(frames["citations"])["mode"] == "vector"


async def test_the_mode_chosen_on_post_is_the_mode_the_search_runs_in_without_the_real_model(
    client, demo_auth, monkeypatch
) -> None:
    """The same carrier property, minus the model, so a cold cache still proves it.

    The demo tenant's corpus already carries real embeddings — `copy_corpus_into`
    writes them from `seed/gdpr_excerpts_vectors.json` at demo-login, no live
    inference involved. The only call the vector leg makes to the embedder is
    encoding the *question*, and that is exactly what is stubbed here: the
    carrier (POST stores the choice, GET reads it, `search_chunks` sees it, the
    frame reports it) is a property of the routing, not of the weights, and
    this proves it without touching them.

    Patches the loaded `_model`, not the loader (`_load`) — the same choice
    `stub_model` in `test_embedding.py` makes, for the same reason: `_ensure()`
    only calls `_load` when `_model is None`, so if an earlier `model`-marked
    test in this process already populated it, patching the loader would leave
    the real weights in place and the stub would never run.
    """

    class _StubRow(list):
        def tolist(self):
            return list(self)

    class _StubModel:
        def embed(self, texts):
            return [_StubRow([0.1] * DIMENSIONS) for _ in texts]

    monkeypatch.setattr(embedding_module, "_model", _StubModel())

    seen, frames = await _post_vector_question_and_observe(client, demo_auth, monkeypatch)

    assert seen == [SearchMode.VECTOR], "the stored choice never reached retrieval"
    assert json.loads(frames["citations"])["mode"] == "vector"


async def test_the_request_default_and_the_column_default_are_the_same_value() -> None:
    """One value in two files stays one value, or the omitted-field path lies."""
    async with system_session() as session:
        column_default = await session.scalar(
            text(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_name = 'messages' AND column_name = 'search_mode'"
            )
        )
    assert SearchMode.FTS.value in column_default
    assert conversations.CreateMessageRequest.model_fields["mode"].default is SearchMode.FTS
