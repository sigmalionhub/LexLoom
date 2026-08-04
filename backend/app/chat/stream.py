import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

import structlog
from starlette.types import Message

from app.chat.gemini import GeminiQuotaExceededError
from app.chat.providers import DEFAULT_GENERATOR, FakeProvider, GeneratorChoice, LLMProvider
from app.chat.retrieval import Citation

logger = structlog.get_logger(__name__)

# Only what sse-starlette does not already decide for us, which is one header.
#
# `Cache-Control` is here because the library applies it with `setdefault`, so
# ours wins: `no-cache` rather than its `no-store`. An SSE body is a live
# connection, not a document — revalidation is the meaningful instruction and it
# is the value the contract test pins.
#
# `Connection: keep-alive` and `X-Accel-Buffering: no` are deliberately absent.
# The library assigns both unconditionally, *after* merging these, so an entry
# here could never change a byte on the wire — it would only look like a
# decision this application makes. On top of that `Connection` is hop-by-hop
# (RFC 9110 §7.6.1): a proxy strips it and writes its own, and HTTP/1.1 keeps
# connections alive by default. Measured rather than assumed for that one:
# uvicorn 0.52 neither rejects nor rewrites it under either HTTP implementation,
# it copies whatever the application sends straight onto the wire — so the
# reason to drop it was never that the stack complains.
#
# The nginx-buffering fix is still shipped and still load-bearing; it is just
# the library's header, not ours, and
# test_sse_headers_carries_only_what_the_library_does_not_set holds that to it.
SSE_HEADERS = {
    "Cache-Control": "no-cache",
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
    the streaming provider: a disconnect cancels the task driving the response
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
    generator: GeneratorChoice = DEFAULT_GENERATOR,
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

    `generator` defaults so the tests in `test_stream_cancellation.py` that
    only care about cancellation, not selection, do not have to name a choice.
    `conversations.py` always passes the one it actually built `provider` from
    — the two must agree, since `model` below is what tells the client which
    generator answered.

    A `GeminiQuotaExceededError` on the first call to `provider.stream()` is
    not treated as failure: it yields an extra `generator_changed` frame —
    always immediately after `citations`, never before, since the error
    surfaces before any token has gone out — and continues the same answer on
    `FakeProvider`. Every other exception still ends the stream with an
    `error` frame and `finish_reason: "error"`, unchanged from before this
    existed.
    """
    progress = progress if progress is not None else StreamProgress()

    yield {
        "event": "message_start",
        # `model` is reused for the generator choice rather than growing a
        # second field: it is the same concept ("what generated this
        # answer"), and two fields for one fact is two sources of truth that
        # can disagree. `str(generator)` rather than the bare `StrEnum` member
        # only to keep the json.dumps output unambiguous about the type — the
        # characters on the wire are identical either way.
        "data": json.dumps({"message_id": message_id, "model": str(generator)}),
    }
    yield {"event": "citations", "data": json.dumps({"chunks": citations})}

    finish_reason = "stop"
    spoken: list[str] = []
    try:
        async for token in provider.stream(prompt):
            spoken.append(token)
            yield {"event": "token", "data": json.dumps({"text": token})}
    except GeminiQuotaExceededError:
        # Quota exhaustion is expected, not exceptional: the free tier allows
        # 10 requests a minute and this is a public demo. Falling back to the
        # deterministic provider keeps the answer coming; saying so in the
        # stream is what keeps it honest. A silent substitution would show the
        # visitor a canned answer they believe came from a model.
        #
        # `httpx` raises this from the first line of the response (the status
        # code arrives before any body), so it always lands here with `spoken`
        # still empty — `generator_changed` therefore always lands exactly
        # where the contract puts it: right after `citations`, before the
        # first `token`. Saboteur check: delete this `except` clause and
        # test_provider_degradation.py goes red on a bare `error` frame
        # instead of a completed, fallen-back answer.
        logger.warning(
            "provider_quota_exceeded", message_id=message_id, generator=str(generator)
        )
        fallback_generator = DEFAULT_GENERATOR
        yield {
            "event": "generator_changed",
            "data": json.dumps(
                {"model": str(fallback_generator), "reason": "quota_exceeded"}
            ),
        }
        try:
            async for token in FakeProvider().stream(prompt):
                spoken.append(token)
                yield {"event": "token", "data": json.dumps({"text": token})}
        except Exception:
            logger.exception("fallback_provider_failed", message_id=message_id)
            yield {
                "event": "error",
                "data": json.dumps(
                    {
                        "code": "provider_unavailable",
                        "message": "Generation failed; sources are above.",
                    }
                ),
            }
            finish_reason = "error"
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
