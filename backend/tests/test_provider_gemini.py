"""Tests for the Gemini adapter's SSE parsing and request shape.

Everything here runs against `httpx.MockTransport` with recorded/synthetic SSE
bytes — no socket, no key needed. The one exception is
`test_a_real_call_streams_text_from_gemini`, which is marked to skip whenever
`GEMINI_API_KEY` is not in the environment, so it never runs in CI (CI has no
key) and is not part of the committed local test run either.
"""

import json
import os

import httpx
import pytest

from app.chat.gemini import GeminiProvider, _text_from_frame, _text_from_sse_line


def _sse_frame(
    text: str | None = None, *, thought: bool = False, finish: bool = False
) -> str:
    """Build one `data: {...}` SSE line shaped like a real streamGenerateContent frame."""
    candidate: dict = {"content": {"role": "model", "parts": []}}
    if text is not None:
        part: dict = {"text": text}
        if thought:
            part["thought"] = True
        candidate["content"]["parts"].append(part)
    if finish:
        candidate["finishReason"] = "STOP"
    frame = {"candidates": [candidate]}
    return f"data: {json.dumps(frame)}"


# Recorded shape, synthesised from the 2026-08-04 measurement noted in
# app/chat/gemini.py: two thought frames ahead of the answer.
_THOUGHT_THEN_ANSWER_SSE = "\n\n".join(
    [
        _sse_frame("Let me look at the sources first.", thought=True),
        _sse_frame("The relevant clause is Article 33(1).", thought=True),
        _sse_frame("Under "),
        _sse_frame("the GDPR, notification is due within 72 hours.", finish=True),
        "",
    ]
).encode()


def _mock_client(handler) -> GeminiProvider:
    return GeminiProvider(model="gemini-3.6-flash", transport=httpx.MockTransport(handler))


async def test_thought_parts_are_dropped_and_answer_parts_are_kept(settings_override) -> None:
    """Saboteur check: remove the `part.get("thought")` guard in
    `_text_from_frame` and this test goes red — the model's reasoning would
    reach the caller mixed in with the answer text."""
    settings_override(gemini_api_key="test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=_THOUGHT_THEN_ANSWER_SSE, headers={"content-type": "text/event-stream"}
        )

    provider = _mock_client(handler)
    tokens = [t async for t in provider.stream("What must be done within 72 hours?")]
    answer = "".join(tokens)

    assert answer == "Under the GDPR, notification is due within 72 hours."
    assert "Let me look" not in answer
    assert "relevant clause" not in answer


async def test_the_api_key_is_sent_as_a_header_never_as_a_query_parameter(
    settings_override,
) -> None:
    """A key in the query string lands in nginx access logs, any proxy in
    front of this box, and shell history — see app/chat/gemini.py."""
    settings_override(gemini_api_key="test-key-not-a-real-secret")
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["header"] = request.headers.get("x-goog-api-key", "")
        seen["url"] = str(request.url)
        return httpx.Response(200, content=b"", headers={"content-type": "text/event-stream"})

    provider = _mock_client(handler)
    [t async for t in provider.stream("q")]

    assert seen["header"] == "test-key-not-a-real-secret"
    assert "test-key-not-a-real-secret" not in seen["url"]
    assert "key=" not in seen["url"]


async def test_the_url_names_streamgeneratecontent_with_alt_sse(settings_override) -> None:
    settings_override(gemini_api_key="test-key")
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, content=b"", headers={"content-type": "text/event-stream"})

    provider = _mock_client(handler)
    [t async for t in provider.stream("q")]

    assert "gemini-3.6-flash:streamGenerateContent" in seen["url"]
    assert "alt=sse" in seen["url"]


def test_text_from_sse_line_ignores_non_data_lines() -> None:
    assert _text_from_sse_line("") is None
    assert _text_from_sse_line(": keep-alive") is None


def test_text_from_frame_concatenates_multiple_answer_parts() -> None:
    frame = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "Under "},
                        {"text": "the GDPR."},
                    ]
                }
            }
        ]
    }
    assert _text_from_frame(frame) == "Under the GDPR."


def test_text_from_frame_returns_empty_string_for_a_frame_with_no_parts() -> None:
    assert _text_from_frame({"candidates": [{"content": {"parts": []}}]}) == ""


@pytest.mark.skipif(
    not os.environ.get("GEMINI_API_KEY"),
    reason="hits the real Gemini API; needs GEMINI_API_KEY, which CI does not have",
)
async def test_a_real_call_streams_text_from_gemini() -> None:
    """Manual, opt-in confirmation that the adapter works end to end.

    Never run as part of the committed suite: CI has no key, and this is the
    one test in the file that is not driven by recorded bytes.
    """
    provider = GeminiProvider(model="gemini-3.6-flash")
    tokens = [
        t
        async for t in provider.stream(
            "Reply with exactly the word 'ok' and nothing else."
        )
    ]
    assert "".join(tokens).strip()
