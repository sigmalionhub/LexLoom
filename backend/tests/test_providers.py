import asyncio

import pytest

from app.chat.providers import FakeProvider


async def test_fake_provider_streams_multiple_tokens() -> None:
    provider = FakeProvider(delay=0.0)
    tokens = [token async for token in provider.stream("What is a data processor?")]
    assert len(tokens) > 3
    assert "".join(tokens).strip()


async def test_the_cancelled_flag_also_notices_a_closed_generator() -> None:
    """`aclose()` throws `GeneratorExit`, which is a sibling of `CancelledError`
    under `BaseException` and not a subclass of it.

    A consumer that closes the stream politely is the common case, and an
    `except asyncio.CancelledError` on its own would never see it — the flag
    would read False on the tidiest exit there is.
    """
    provider = FakeProvider(delay=0.0)
    stream = provider.stream("long answer")
    await stream.__anext__()
    await stream.aclose()
    assert provider.cancelled is True


async def test_provider_observes_cancellation() -> None:
    """Cancellation must reach the provider. A sync SDK call dispatched to a
    threadpool cannot be cancelled and would keep burning tokens after the
    client is gone."""
    provider = FakeProvider(delay=0.05)

    async def consume() -> None:
        async for _ in provider.stream("long answer"):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.12)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.cancelled is True
