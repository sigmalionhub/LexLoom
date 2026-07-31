import asyncio
from collections.abc import AsyncIterator
from typing import Protocol

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
        self.cancelled = False

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        try:
            for word in _CANNED_ANSWER.split(" "):
                await asyncio.sleep(self._delay)
                yield word + " "
        except asyncio.CancelledError:
            self.cancelled = True
            raise
