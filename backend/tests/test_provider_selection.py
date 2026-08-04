"""The generator a visitor picks is the generator that answers.

`GeneratorChoice` (`app/chat/providers.py`) is the single carrier for the
value: `POST /messages` validates and stores it, `GET .../stream` reads it back
off the same row it already selects, builds the matching `LLMProvider`, and
reports it on the `message_start` frame's `model` field. Everything below is
either a unit test of that mapping (`build_provider`) or the end-to-end proof
that a choice made at POST time is the choice the stream reports — the test a
`202 Accepted` alone cannot stand in for, per this task's brief.
"""

import json

import pytest

from app.chat.gemini import GeminiProvider
from app.chat.providers import (
    DEFAULT_GENERATOR,
    FakeProvider,
    GeneratorChoice,
    build_provider,
)


def test_default_generator_is_deterministic() -> None:
    """Pinned to the literal choice, not "the first enum member" — reordering
    the class body in `providers.py` must not silently change what a request
    with no opinion gets."""
    assert DEFAULT_GENERATOR is GeneratorChoice.DETERMINISTIC


def test_generator_choice_has_exactly_the_three_contracted_members() -> None:
    assert {member.value for member in GeneratorChoice} == {
        "deterministic",
        "gemini-3.5-flash-lite",
        "gemini-3.6-flash",
    }


def test_build_provider_defaults_to_the_fake_provider() -> None:
    assert isinstance(build_provider(), FakeProvider)


def test_build_provider_returns_the_fake_provider_for_deterministic() -> None:
    assert isinstance(build_provider(GeneratorChoice.DETERMINISTIC), FakeProvider)


@pytest.mark.parametrize(
    "choice", [GeneratorChoice.FLASH_LITE, GeneratorChoice.FLASH]
)
def test_build_provider_refuses_a_gemini_choice_with_no_api_key(
    choice, settings_override
) -> None:
    settings_override(gemini_api_key="")
    with pytest.raises(ValueError, match=choice.name):
        build_provider(choice)


@pytest.mark.parametrize(
    ("choice", "expected_model"),
    [
        (GeneratorChoice.FLASH_LITE, "gemini-3.5-flash-lite"),
        (GeneratorChoice.FLASH, "gemini-3.6-flash"),
    ],
)
def test_build_provider_builds_the_gemini_model_the_choice_names(
    choice, expected_model, settings_override
) -> None:
    """The model `build_provider` hands `GeminiProvider` is read off the
    choice's own value, not a second, separately-maintained mapping — this is
    what Task 29's controller ruling replaced the old hardcoded
    `_DEFAULT_GEMINI_MODEL` with."""
    settings_override(gemini_api_key="test-key-not-a-real-secret")
    provider = build_provider(choice)
    assert isinstance(provider, GeminiProvider)
    assert provider._model == expected_model


async def test_stream_reports_the_generator_that_was_selected_at_post_time(
    client, demo_auth, settings_override
) -> None:
    """The end-to-end proof: POST with `generator=gemini-3.5-flash-lite`, then
    GET the stream, and check the *first* frame — `message_start` — carries
    that exact value on `model`.

    A fake API key is enough: `message_start` is emitted before
    `provider.stream()` is ever awaited, and this test closes the connection
    the moment it has read that frame, so the (fake) Gemini call is never
    made — no network access, no hang, nothing to mock.

    Sabotage for this test, matching the brief's Step 9: remove the `generator`
    column from the `SELECT` in `stream_message` (or stop passing it into
    `generate_events`) and this goes red on the assertion below, because the
    frame would report `deterministic` regardless of what was posted.
    """
    settings_override(gemini_api_key="test-key-not-a-real-secret")

    conversation_id = (
        await client.post("/api/v1/conversations", json={"title": "Demo"}, headers=demo_auth)
    ).json()["id"]
    message_id = (
        await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"content": "q", "generator": "gemini-3.5-flash-lite"},
            headers=demo_auth,
        )
    ).json()["message_id"]
    ticket = (
        await client.post(
            "/api/v1/auth/stream-tickets", json={"message_id": message_id}, headers=demo_auth
        )
    ).json()["ticket"]

    url = (
        f"/api/v1/conversations/{conversation_id}/messages/{message_id}"
        f"/stream?ticket={ticket}"
    )
    model_seen = None
    async with client.stream("GET", url) as response:
        pending_event = None
        async for line in response.aiter_lines():
            if line.startswith("event: "):
                pending_event = line.removeprefix("event: ").strip()
            elif line.startswith("data: ") and pending_event == "message_start":
                model_seen = json.loads(line.removeprefix("data: "))["model"]
                break

    assert model_seen == "gemini-3.5-flash-lite"


async def test_a_post_with_no_generator_field_defaults_to_deterministic(
    client, demo_auth
) -> None:
    """The other half of the same path, at the other end of the enum: a caller
    that never mentions `generator` at all gets the canned demo answer, and the
    stream says so plainly rather than leaving the field to guesswork."""
    conversation_id = (
        await client.post("/api/v1/conversations", json={"title": "Demo"}, headers=demo_auth)
    ).json()["id"]
    message_id = (
        await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"content": "q"},
            headers=demo_auth,
        )
    ).json()["message_id"]
    ticket = (
        await client.post(
            "/api/v1/auth/stream-tickets", json={"message_id": message_id}, headers=demo_auth
        )
    ).json()["ticket"]

    url = (
        f"/api/v1/conversations/{conversation_id}/messages/{message_id}"
        f"/stream?ticket={ticket}"
    )
    model_seen = None
    async with client.stream("GET", url) as response:
        pending_event = None
        async for line in response.aiter_lines():
            if line.startswith("event: "):
                pending_event = line.removeprefix("event: ").strip()
            elif line.startswith("data: ") and pending_event == "message_start":
                model_seen = json.loads(line.removeprefix("data: "))["model"]
                break

    assert model_seen == "deterministic"


async def test_an_unknown_generator_value_is_rejected_at_post_not_at_stream_time(
    client, demo_auth
) -> None:
    conversation_id = (
        await client.post("/api/v1/conversations", json={"title": "Demo"}, headers=demo_auth)
    ).json()["id"]
    response = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": "q", "generator": "gpt-5"},
        headers=demo_auth,
    )
    assert response.status_code == 422
