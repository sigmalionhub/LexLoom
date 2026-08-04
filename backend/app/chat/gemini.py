"""Adapter from `LLMProvider` onto Gemini's streaming REST endpoint.

A sibling module to `providers.py`, deliberately — `providers.py` is a single
file, not a package, and `from app.chat.providers import FakeProvider` in
`conversations.py` would break the moment a `providers/__init__.py` shadowed
it. `providers.py` imports `GeminiProvider` from here rather than the other
way around.
"""

import json
from collections.abc import AsyncIterator

import httpx

from app.config import get_settings

# `streamGenerateContent` with `alt=sse`, not the newer `/v1beta/interactions`
# surface — that one takes `input` instead of `contents`, and nothing here
# needs it.
_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:streamGenerateContent?alt=sse"
)

# Generous on read: the model may sit silent for a while before the first
# token, and once streaming starts each subsequent line is cheap to wait for.
# Modest on connect: a DNS or TCP failure should surface quickly rather than
# hang the whole admission-controlled slot it is running under.
_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)


class GeminiProvider:
    """`LLMProvider` backed by a live call to the Gemini API.

    Implements the existing protocol without changing it — `stream()` takes
    the ready prompt string `build_grounded_prompt` produced and yields text
    chunks. It does not build the prompt, does not touch the database, and
    does not know what a citation is; that separation is what keeps
    `FakeProvider` swappable for this with nothing else in the request path
    changing.
    """

    def __init__(self, model: str, *, transport: httpx.BaseTransport | None = None) -> None:
        self._model = model
        # Injectable only for tests: production callers never pass this, and
        # `httpx.AsyncClient(transport=None)` is the same as omitting it. It is
        # what lets test_provider_gemini.py drive this class against recorded
        # SSE bytes with no socket and no key.
        self._transport = transport

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        settings = get_settings()
        url = _URL.format(model=self._model)
        payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
        headers = {
            # Header, never a query parameter: a key on the URL lands in nginx
            # access logs, in any proxy in front of this box, and in shell
            # history for anyone who copies the request to reproduce it.
            "x-goog-api-key": settings.gemini_api_key,
            "content-type": "application/json",
        }
        async with httpx.AsyncClient(timeout=_TIMEOUT, transport=self._transport) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    text = _text_from_sse_line(line)
                    if text:
                        yield text


def _text_from_sse_line(line: str) -> str | None:
    """Pull generated text out of one `data: {...}` SSE frame.

    Returns `None` for anything that is not a data frame (blank keep-alive
    lines, and Gemini sends no other kind here).
    """
    if not line.startswith("data:"):
        return None
    raw = line.removeprefix("data:").strip()
    if not raw:
        return None
    frame = json.loads(raw)
    return _text_from_frame(frame) or None


def _text_from_frame(frame: dict) -> str:
    """Concatenate the answer text in one frame, dropping reasoning.

    `part.get("thought")` marks a part as the model's internal reasoning, not
    its answer. Measured 2026-08-04: `gemini-3.6-flash` sends two thought
    frames ahead of the answer. Without this check, that reasoning would reach
    the user as if it were the response.
    """
    pieces: list[str] = []
    for candidate in frame.get("candidates", []):
        content = candidate.get("content") or {}
        for part in content.get("parts", []):
            if part.get("thought"):
                continue
            text = part.get("text")
            if text:
                pieces.append(text)
    return "".join(pieces)
