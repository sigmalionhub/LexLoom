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

# HTTP status Gemini answers with once the free tier's per-minute quota is
# spent. Checked on the response before `raise_for_status()` runs, not caught
# as an `httpx.HTTPStatusError` afterwards: the caller (`app.chat.stream`) has
# to tell "the model is temporarily out of quota, keep the demo going on the
# deterministic provider" apart from every other way this call can fail, and a
# bare `HTTPStatusError` carries no such distinction on its own.
_QUOTA_EXHAUSTED_STATUS = 429


class GeminiQuotaExceededError(Exception):
    """Gemini refused the request with HTTP 429 — the free tier's rate limit.

    Expected, not exceptional: the free tier allows 10 requests a minute and
    this is a public demo. `app.chat.stream.generate_events` catches this
    specific type and falls back to `FakeProvider` rather than ending the
    stream in an `error` frame, which is what happens to every other
    exception this class raises.
    """


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
                if response.status_code == _QUOTA_EXHAUSTED_STATUS:
                    # Raised before `raise_for_status()` sees it: that call
                    # would raise `httpx.HTTPStatusError` too, but the caller
                    # needs this one case named so it can degrade instead of
                    # failing the stream.
                    raise GeminiQuotaExceededError(
                        f"Gemini quota exhausted for model {self._model!r} (HTTP 429)"
                    )
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
