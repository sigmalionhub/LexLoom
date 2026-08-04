import asyncio
from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Protocol

from app.chat.gemini import GeminiProvider
from app.config import get_settings

_CANNED_ANSWER = (
    "Under the referenced agreement, the processor must implement appropriate "
    "technical and organisational measures, and may not engage a sub-processor "
    "without the controller's prior written authorisation. The cited clauses "
    "set out the notification window and the audit rights that follow from it."
)


class LLMProvider(Protocol):
    """Generation is behind an interface so the demo runs with zero API keys.

    Swapping in a real provider changes this one implementation and nothing
    else — the SSE contract, cancellation and citation flow are unaffected.
    """

    def stream(self, prompt: str) -> AsyncIterator[str]: ...


class FakeProvider:
    """Deterministic streaming provider used by the public demo and by tests."""

    def __init__(self, delay: float = 0.03) -> None:
        self._delay = delay
        # Approximate, and only ever read by tests of this class. It records
        # that generation was stopped *while this frame was running*, which is
        # not the same question as "was the stream cancelled": a consumer that
        # abandons the generator without closing it leaves this frame suspended
        # and never finalised in observable time, so the flag stays False while
        # generation has plainly stopped. The stream's own cancellation
        # bookkeeping therefore lives in app.chat.stream, on the ASGI
        # disconnect, and reads nothing from here.
        self.cancelled = False

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        try:
            for word in _CANNED_ANSWER.split(" "):
                await asyncio.sleep(self._delay)
                yield word + " "
        except (asyncio.CancelledError, GeneratorExit):
            # GeneratorExit as well as CancelledError: `aclose()` — what a tidy
            # consumer calls — throws the former, and the two are siblings under
            # BaseException, so catching only CancelledError misses the polite
            # exit entirely.
            self.cancelled = True
            raise


class GeneratorChoice(StrEnum):
    """Which `LLMProvider` a request asked for.

    Not read by any endpoint yet — `conversations.py` still always builds a
    `FakeProvider` (Task 28's brief is explicit that those two lines are the
    only ones this task touches there). This exists so the value has one
    definition that a later task (Task 30, which owns the request shape and
    `main.py`) imports rather than inventing its own string literal — and so
    the OpenAPI schema it generates gets a real enum instead of a bare `str`.
    """

    FAKE = "fake"
    GEMINI = "gemini"


# Hardcoded rather than a `Settings` field: nothing in this task's scope lets
# an operator choose a different Gemini model, and `.env.example` only grew a
# key for the credential (Step 5) — a model knob with no reader would be a
# knob that turns nothing. `gemini-3.6-flash` is also the model the SSE
# parsing in `app.chat.gemini` was measured against on 2026-08-04.
_DEFAULT_GEMINI_MODEL = "gemini-3.6-flash"


def build_provider(choice: GeneratorChoice = GeneratorChoice.FAKE) -> LLMProvider:
    """Construct the provider a request asked for.

    Raises `ValueError` for `GEMINI` when `GEMINI_API_KEY` is unset rather than
    silently falling back to `FakeProvider` — a caller who asked for a real
    model and silently got the canned demo answer would have no way to notice.
    Whoever offers `GEMINI` as a choice (Task 30) is responsible for not
    offering it when the key is absent; this function only refuses to
    misrepresent what it built.
    """
    if choice is GeneratorChoice.FAKE:
        return FakeProvider()
    settings = get_settings()
    if not settings.gemini_api_key:
        raise ValueError(
            "GeneratorChoice.GEMINI was requested but GEMINI_API_KEY is not configured"
        )
    return GeminiProvider(model=_DEFAULT_GEMINI_MODEL)
