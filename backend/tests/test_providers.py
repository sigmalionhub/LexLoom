import asyncio

import pytest

from app.chat.providers import FakeProvider


async def test_fake_provider_streams_multiple_tokens() -> None:
    provider = FakeProvider(delay=0.0)
    tokens = [token async for token in provider.stream("What is a data processor?")]
    assert len(tokens) > 3
    assert "".join(tokens).strip()


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
