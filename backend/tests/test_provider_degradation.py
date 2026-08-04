"""What happens when the real model cannot answer: a quota refusal, or no
`GEMINI_API_KEY` at all.

Two failure modes, two different treatments, on purpose:

* Quota exhaustion (`GeminiQuotaExceededError`, HTTP 429 from Gemini) is
  expected on a public demo running the free tier, so it degrades *inside*
  the stream — a `generator_changed` frame, then the same answer on
  `FakeProvider`. The stream still ends `finish_reason: "stop"`.
* No key at all is refused *before* the stream exists, with a named
  `422 generator_unavailable` — never the generic `500 internal_error` an
  uncaught `ValueError` from `build_provider` produced before this task, and
  never a stream that has already committed to `200` and cannot say so.

`GET /api/v1/generators` is what is supposed to keep a visitor from reaching
either path through the UI: it lists only the generators that would actually
work right now, so a clone with `GEMINI_API_KEY=""` still runs and still
chats — it just never offers a Gemini option to click.
"""

import json

from app.chat.gemini import GeminiQuotaExceededError
from app.chat.providers import DEFAULT_GENERATOR, GeneratorChoice
from app.chat.stream import generate_events


class _QuotaExceededProvider:
    """Fails exactly the way `GeminiProvider` does: on the first thing the
    caller awaits, before any token — because Gemini's HTTP status arrives on
    the response before any of the body is read, `raise_for_status()`-style.
    """

    async def stream(self, prompt: str):
        raise GeminiQuotaExceededError("test: quota exhausted")
        yield ""  # pragma: no cover - unreachable; makes this an async generator


async def test_a_quota_error_degrades_to_the_deterministic_provider_after_citations() -> None:
    """Saboteur check: delete the `except GeminiQuotaExceededError` clause in
    `app/chat/stream.py` and this goes red — the stream ends on a bare
    `error` frame with `finish_reason: "error"` instead of a completed answer.
    """
    seen: list[dict] = []
    async for event in generate_events(
        message_id="m1",
        provider=_QuotaExceededProvider(),
        citations=[],
        prompt="q",
        generator=GeneratorChoice.FLASH_LITE,
    ):
        seen.append(event)

    names = [event["event"] for event in seen]
    assert names[0] == "message_start"
    assert names[1] == "citations"
    # Right after citations, never before, and strictly before the first
    # token — the position the contract pins.
    assert names[2] == "generator_changed"
    assert names.index("generator_changed") < names.index("token")

    changed = json.loads(seen[2]["data"])
    assert changed["model"] == str(DEFAULT_GENERATOR)
    assert changed["reason"] == "quota_exceeded"

    assert names[-1] == "message_end"
    finish = json.loads(seen[-1]["data"])
    # Degraded, not failed: the visitor got an answer.
    assert finish["finish_reason"] == "stop"

    spoken = "".join(
        json.loads(event["data"])["text"] for event in seen if event["event"] == "token"
    )
    assert spoken.strip(), "the fallback provider produced no answer at all"

    # test_sse_contract.py:38,42 pins message_start and citations as frames 0
    # and 1 of the ordinary path; nothing about this path changes that, since
    # generator_changed only ever appears when a Gemini call actually failed.
    assert "error" not in names


async def test_generators_endpoint_lists_only_deterministic_with_no_key(
    client, settings_override
) -> None:
    """The literal check behind the README's "Runs with zero API keys": a
    clone with an empty key still has a working chat option to offer."""
    settings_override(gemini_api_key="")

    response = await client.get("/api/v1/generators")

    assert response.status_code == 200
    listed = response.json()["generators"]
    assert [item["value"] for item in listed] == ["deterministic"]


async def test_generators_endpoint_lists_all_three_with_a_key(
    client, settings_override
) -> None:
    settings_override(gemini_api_key="test-key-not-a-real-secret")

    response = await client.get("/api/v1/generators")

    assert response.status_code == 200
    listed = {item["value"] for item in response.json()["generators"]}
    assert listed == {"deterministic", "gemini-3.5-flash-lite", "gemini-3.6-flash"}


async def test_chat_still_works_end_to_end_with_no_gemini_api_key(
    client, demo_auth, settings_override
) -> None:
    """`GET /api/v1/generators` narrows what the UI offers; this is the proof
    that the rest of the demo — asking a question with the default,
    deterministic generator — does not need the key at all."""
    settings_override(gemini_api_key="")

    conversation_id = (
        await client.post("/api/v1/conversations", json={"title": "Demo"}, headers=demo_auth)
    ).json()["id"]
    message_id = (
        await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"content": "q"},
            headers=demo_auth,
        )
    ).json()["message_id"]
    ticket = (
        await client.post(
            "/api/v1/auth/stream-tickets", json={"message_id": message_id}, headers=demo_auth
        )
    ).json()["ticket"]

    url = (
        f"/api/v1/conversations/{conversation_id}/messages/{message_id}/stream?ticket={ticket}"
    )
    async with client.stream("GET", url) as response:
        assert response.status_code == 200
        events = [
            line.removeprefix("event: ").strip()
            async for line in response.aiter_lines()
            if line.startswith("event: ")
        ]

    assert events[-1] == "message_end"
    assert "error" not in events


async def test_picking_a_gemini_generator_with_no_key_fails_with_a_named_code(
    client, demo_auth, settings_override
) -> None:
    """The residual path `GET /api/v1/generators` cannot close by itself: a
    caller that posts a Gemini choice directly (bypassing the selector, or
    caught between the key being removed and the frontend noticing) must not
    see `500 internal_error` — that told a real reviewer nothing (Task 29's
    review, closed here). It sees a named, actionable code instead.
    """
    settings_override(gemini_api_key="")

    conversation_id = (
        await client.post("/api/v1/conversations", json={"title": "Demo"}, headers=demo_auth)
    ).json()["id"]
    message_id = (
        await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"content": "q", "generator": "gemini-3.5-flash-lite"},
            headers=demo_auth,
        )
    ).json()["message_id"]
    ticket = (
        await client.post(
            "/api/v1/auth/stream-tickets", json={"message_id": message_id}, headers=demo_auth
        )
    ).json()["ticket"]

    response = await client.get(
        f"/api/v1/conversations/{conversation_id}/messages/{message_id}/stream?ticket={ticket}"
    )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "generator_unavailable"
    assert body["code"] != "internal_error"
