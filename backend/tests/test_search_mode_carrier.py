"""The mode chosen on POST is the mode the search actually runs in.

This is the carrier test, not a retrieval test: `test_retrieval_modes.py` shows
`HYBRID`/`VECTOR`/`FTS` are different searches. This file shows the choice a
visitor makes on `POST /messages` survives the round trip through
`messages.search_mode` and reaches `search_chunks` unchanged, and that the
frame reports the mode the answer was actually retrieved with.
"""

import json

from sqlalchemy import text

from app.api.v1 import conversations
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
    """
    seen: list[SearchMode] = []
    real = conversations.search_chunks

    async def recording(session, query, *, mode, limit=5):
        seen.append(mode)
        return await real(session, query, mode=mode, limit=limit)

    monkeypatch.setattr(conversations, "search_chunks", recording)

    conversation_id = await _create_conversation(client, demo_auth)
    message_id = (
        await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"content": "What are the processor's obligations?", "mode": "vector"},
            headers=demo_auth,
        )
    ).json()["message_id"]
    frames = await _collect_frames(client, demo_auth, conversation_id, message_id)

    assert seen == [SearchMode.VECTOR], "the stored choice never reached retrieval"
    # And the visitor can see it: the frame reports the mode the answer was
    # actually retrieved with, which is what makes a broken carrier visible
    # instead of merely wrong.
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
