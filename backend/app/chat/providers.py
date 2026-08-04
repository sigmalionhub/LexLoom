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

    Read on the `POST /messages` request body and stored on the row (migration
    0005's `generator` column); `stream_message` reads it back and hands it to
    `build_provider`. The value that comes back out is also what the
    `message_start` SSE frame carries as `model` — see `app.chat.stream`.

    The two Gemini members carry the model id as their value rather than a
    short label like the old `GEMINI` member did: `build_provider` below reads
    the model straight off `choice.value`, so there is exactly one place that
    knows the mapping from a visitor's choice to a Gemini model name, and it
    is this enum. A short label plus a separate lookup table would be two
    things that have to agree with each other and nothing enforcing it.
    """

    DETERMINISTIC = "deterministic"
    FLASH_LITE = "gemini-3.5-flash-lite"
    FLASH = "gemini-3.6-flash"


# Not "the first member of the enum": member order is display/declaration
# order, not a contract, and reordering the class body must not silently
# change which provider a request with no opinion gets.
# test_default_generator_is_deterministic pins this to the literal choice.
DEFAULT_GENERATOR = GeneratorChoice.DETERMINISTIC


def build_provider(choice: GeneratorChoice = DEFAULT_GENERATOR) -> LLMProvider:
    """Construct the provider a request asked for.

    Raises `ValueError` for a Gemini choice when `GEMINI_API_KEY` is unset
    rather than silently falling back to `FakeProvider` — a caller who asked
    for a real model and silently got the canned demo answer would have no way
    to notice. Whoever offers a Gemini choice in the UI is responsible for not
    offering it when the key is absent; this function only refuses to
    misrepresent what it built.
    """
    if choice is GeneratorChoice.DETERMINISTIC:
        return FakeProvider()
    settings = get_settings()
    if not settings.gemini_api_key:
        raise ValueError(
            f"GeneratorChoice.{choice.name} was requested but GEMINI_API_KEY is not configured"
        )
    return GeminiProvider(model=choice.value)
