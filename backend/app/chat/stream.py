import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

import structlog
from starlette.types import Message

from app.chat.providers import LLMProvider
from app.chat.retrieval import Citation

logger = structlog.get_logger(__name__)

MODEL_NAME = "fake-1"

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    # nginx buffers proxied responses by default. Without this header the
    # client receives one flush at the end, which is the bug this endpoint
    # exists to fix.
    "X-Accel-Buffering": "no",
    # `Connection: keep-alive` is deliberately absent. It is hop-by-hop
    # (RFC 9110 §7.6.1): it describes one leg of the path, so a proxy strips it
    # and writes its own, and on HTTP/1.1 persistent connections are the default
    # anyway. Measured rather than assumed: uvicorn 0.52 neither rejects nor
    # rewrites it under either HTTP implementation — it copies whatever the
    # application sends straight onto the wire — so the reason to drop it is
    # that it claims a decision the application does not get to make, not that
    # the stack complains. sse-starlette sets the header itself regardless, so
    # listing it here could never have changed a byte.
}


@dataclass
class StreamProgress:
    """Did the consumer receive every event this stream promised?

    Shared between the generator and the disconnect handler because neither can
    answer the question alone: the generator knows when it finished, and only
    the ASGI receive channel knows when the client left.
    """

    completed: bool = False


def client_close_handler(
    *, message_id: str, progress: StreamProgress
) -> Callable[[Message], Awaitable[None]]:
    """Log the client walking out mid-answer, once, from where it is observable.

    This is the *only* place a cancellation is recorded, and it is not the
    obvious place. The obvious place — `except asyncio.CancelledError` inside
    the generator — cannot be relied on. Measured on this stack while building
    the provider (Task 13): a disconnect cancels the task driving the response
    wherever it happens to be suspended, and with uvicorn advertising ASGI
    spec_version 2.3 a client that goes away under backpressure leaves it
    suspended in the socket write rather than in the generator, which is then
    never resumed. A tidy consumer calling `aclose()` throws `GeneratorExit`
    instead, a sibling of `CancelledError` under `BaseException`, which that
    clause would not catch either.

    `http.disconnect` has neither problem: the server delivers it on the receive
    channel regardless of where the response task is parked. What this module
    asserts about all of it is the outcome an operator depends on — a dropped
    connection produces exactly one line naming the message, a completed one
    produces none — and it is asserted against a real uvicorn and a real socket
    in test_only_the_client_that_left_early_is_reported_as_cancelled.
    """

    async def _on_client_close(_message: Message) -> None:
        if progress.completed:
            # The disconnect message arrives on the happy path too. uvicorn
            # wakes the receive channel as soon as the response is complete and
            # answers `http.disconnect` from then on, so without this flag every
            # successful stream would log a cancellation and the log line would
            # mean nothing.
            return
        logger.info("stream_cancelled_by_client", message_id=message_id)

    return _on_client_close


async def generate_events(
    *,
    message_id: str,
    provider: LLMProvider,
    citations: list[Citation],
    prompt: str,
    progress: StreamProgress | None = None,
    on_complete: Callable[[str, str], Awaitable[None]] | None = None,
) -> AsyncIterator[dict]:
    """Yield the SSE contract for one answer: start, sources, tokens, end.

    Cancellation is not handled here on purpose — see `client_close_handler`
    for where it is handled and why it cannot be here. The practical
    consequence is the one that matters: on cancellation this generator is
    abandoned mid-loop, so `on_complete` never runs and nothing is stored. An
    answer nobody waited for is not an answer, and a write attempted from a
    cancelled task is itself cancelled, so there is nothing to salvage by
    trying.
    """
    progress = progress if progress is not None else StreamProgress()

    yield {
        "event": "message_start",
        "data": json.dumps({"message_id": message_id, "model": MODEL_NAME}),
    }
    yield {"event": "citations", "data": json.dumps({"chunks": citations})}

    finish_reason = "stop"
    spoken: list[str] = []
    try:
        async for token in provider.stream(prompt):
            spoken.append(token)
            yield {"event": "token", "data": json.dumps({"text": token})}
    except Exception:
        logger.exception("provider_failed", message_id=message_id)
        yield {
            "event": "error",
            "data": json.dumps(
                {"code": "provider_unavailable", "message": "Generation failed; sources are above."}
            ),
        }
        finish_reason = "error"

    if on_complete is not None:
        try:
            await on_complete("".join(spoken), finish_reason)
        except Exception:
            # The client has the answer already; turning a storage failure into
            # a failed stream would take away the one thing that did work.
            logger.exception("stream_result_not_stored", message_id=message_id)

    yield {"event": "message_end", "data": json.dumps({"finish_reason": finish_reason})}
    # Reached only when the consumer comes back for the event after
    # `message_end`, which is the strongest statement this frame can make: every
    # event the contract promises has been handed to the transport.
    progress.completed = True
