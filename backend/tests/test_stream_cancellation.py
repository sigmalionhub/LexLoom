import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from app.chat.providers import FakeProvider
from app.chat.stream import generate_events


async def test_cancelling_the_consumer_stops_generation() -> None:
    """Cancelling the task that drives the stream must stop the provider.

    Asserted by counting tokens rather than by reading `provider.cancelled`.
    The flag is approximate by construction (see `FakeProvider`); what the
    endpoint owes its operator is that generation stopped, and a short answer
    is that, measured.
    """
    provider = FakeProvider(delay=0.05)
    seen: list[dict] = []
    stored: list[tuple[str, str]] = []

    async def store(answer: str, finish_reason: str) -> None:
        stored.append((answer, finish_reason))

    async def consume() -> None:
        async for event in generate_events(
            message_id="m1", provider=provider, citations=[], prompt="q", on_complete=store
        ):
            seen.append(event)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    whole_answer = len([token async for token in FakeProvider(delay=0.0).stream("q")])
    spoken = [event for event in seen if event["event"] == "token"]
    assert spoken, "cancelled before the first token, so this proves nothing"
    assert len(spoken) < whole_answer

    assert not any(event["event"] == "message_end" for event in seen)
    # Nothing is stored for an answer nobody stayed for; the question keeps its
    # 'pending' status, which is then the literal truth about it.
    assert stored == []


async def test_closing_the_stream_stops_generation_without_finishing_it() -> None:
    """The tidy consumer's exit path is `aclose()`, which throws `GeneratorExit`.

    It is a sibling of `CancelledError` under `BaseException`, so an
    `except asyncio.CancelledError` clause would miss it entirely. This test
    exists to keep that path exercised, because it is the one a well-behaved
    client library takes and the one people forget.
    """
    provider = FakeProvider(delay=0.05)
    stored: list[tuple[str, str]] = []

    async def store(answer: str, finish_reason: str) -> None:
        stored.append((answer, finish_reason))

    events = generate_events(
        message_id="m1", provider=provider, citations=[], prompt="q", on_complete=store
    )
    seen: list[str] = []
    async for event in events:
        seen.append(event["event"])
        if event["event"] == "token":
            break
    await events.aclose()

    assert seen == ["message_start", "citations", "token"]
    assert stored == []


async def test_provider_failure_emits_an_error_event_then_ends() -> None:
    class Broken:
        async def stream(self, prompt: str):
            raise RuntimeError("upstream 500")
            yield  # pragma: no cover

    events = [
        e["event"]
        async for e in generate_events(
            message_id="m1", provider=Broken(), citations=[], prompt="q"
        )
    ]
    assert events == ["message_start", "citations", "error", "message_end"]


async def test_a_provider_failure_is_still_stored_as_a_finished_answer() -> None:
    """An error is an outcome, not an absence.

    The question must not stay 'pending' after the endpoint has told the client
    that generation failed — that is the state reserved for answers nobody
    waited for, and conflating the two makes both unreadable.
    """

    class Broken:
        async def stream(self, prompt: str):
            raise RuntimeError("upstream 500")
            yield  # pragma: no cover

    stored: list[tuple[str, str]] = []

    async def store(answer: str, finish_reason: str) -> None:
        stored.append((answer, finish_reason))

    async for _ in generate_events(
        message_id="m1", provider=Broken(), citations=[], prompt="q", on_complete=store
    ):
        pass

    assert stored == [("", "error")]


async def test_a_failure_mid_answer_keeps_the_tokens_and_names_the_reason() -> None:
    """The failure path's *payload* is a contract, not just its event names.

    The two tests above assert the sequence of `event` names and that something
    is stored. Neither reads a single `data` field, so the wire contract the
    frontend actually switches on — `code` on the `error` event and
    `finish_reason` on `message_end` — was unguarded: renaming
    `provider_unavailable` or writing `finish_reason: "stop"` on a failed
    generation left both of them green.

    Both providers above also raise before yielding anything, so the answer
    under test is always `""`. That is the one case where "the partial answer is
    kept" cannot be distinguished from "nothing is kept". This provider speaks
    first and fails afterwards, which is what a real one does when an upstream
    drops mid-generation, and it is the only shape that can tell those apart.
    """

    class DiesMidSentence:
        async def stream(self, prompt: str):
            yield "Under "
            yield "the "
            raise RuntimeError("upstream closed the connection")

    stored: list[tuple[str, str]] = []

    async def store(answer: str, finish_reason: str) -> None:
        stored.append((answer, finish_reason))

    seen = [
        event
        async for event in generate_events(
            message_id="m1",
            provider=DiesMidSentence(),
            citations=[],
            prompt="q",
            on_complete=store,
        )
    ]

    assert [event["event"] for event in seen] == [
        "message_start",
        "citations",
        "token",
        "token",
        "error",
        "message_end",
    ]

    # The client is told what happened in terms it can switch on, and is told
    # nothing about the exception. "upstream closed the connection" is the
    # operator's business; the log carries it and the stream does not.
    error = json.loads(next(e for e in seen if e["event"] == "error")["data"])
    assert error == {
        "code": "provider_unavailable",
        "message": "Generation failed; sources are above.",
    }
    assert "upstream closed the connection" not in json.dumps(seen)

    # `message_end` still arrives, and it says the answer ended badly. A stream
    # that simply stopped would leave a UI spinning forever.
    assert json.loads(seen[-1]["data"]) == {"finish_reason": "error"}

    # What was generated before the failure is kept. The alternative — discard
    # a half-answer because it is incomplete — throws away the only part that
    # worked, and it is why `finish_reason` exists to qualify it.
    assert stored == [("Under the ", "error")]


# --------------------------------------------------------------------------
# The rest of this module runs against a real uvicorn process.
#
# A cancelled asyncio task and a client that drops its socket are two different
# deliveries, and only the second one is what a closing browser tab produces.
# The ASGI transport used everywhere else in this suite cannot produce it: it
# calls the application in-process and only reports `http.disconnect` once the
# response is already complete. So the one behaviour this whole project exists
# to demonstrate would otherwise be tested by proxy, which is how it comes to
# be broken in the browser while the suite is green.
# --------------------------------------------------------------------------

_BACKEND_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class LiveServer:
    port: int
    output: list[str] = field(default_factory=list)

    def events(self, name: str, message_id: str) -> list[dict]:
        """Structured log lines of one kind, about one message."""
        found = []
        for line in list(self.output):
            try:
                record = json.loads(line)
            except ValueError:
                continue  # uvicorn's own lines are not JSON
            if record.get("event") == name and record.get("message_id") == message_id:
                found.append(record)
        return found

    def wait_for(self, name: str, message_id: str, timeout: float = 15.0) -> list[dict]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = self.events(name, message_id)
            if found:
                return found
            time.sleep(0.05)
        raise AssertionError(
            f"no {name!r} line for message {message_id} within {timeout}s. Server said:\n"
            + "".join(self.output[-40:])
        )


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="module")
def live_server():
    """Run the application under uvicorn and capture everything it prints.

    AUTH_JWKS_URL is pointed at this process's own JWKS endpoint. The default
    names port 8100, which is the Compose `api` container — a different process
    whose dev IdP holds a different in-memory keypair, so tokens minted here
    would be rejected by a key set fetched from there.
    """
    port = _free_port()
    process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell, test-only
        [
            sys.executable, "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
        ],
        cwd=_BACKEND_ROOT,
        env={
            **os.environ,
            "PYTHONUNBUFFERED": "1",
            "AUTH_JWKS_URL": f"http://127.0.0.1:{port}/.well-known/jwks.json",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    server = LiveServer(port=port)
    threading.Thread(
        target=lambda: [server.output.append(line) for line in process.stdout], daemon=True
    ).start()

    deadline = time.monotonic() + 30
    while True:
        if process.poll() is not None:
            raise AssertionError("uvicorn exited during startup:\n" + "".join(server.output))
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health/live", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline:
            raise AssertionError("uvicorn never became live:\n" + "".join(server.output))
        time.sleep(0.1)

    yield server

    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - only on a wedged server
        process.kill()


async def _ask(client: httpx.AsyncClient, created_orgs: list[str]) -> tuple[str, str, str]:
    """Log in, open a conversation, ask a question, take a ticket for it."""
    login = (
        await client.post("/api/v1/auth/demo-login", json={"label": "Acme Legal"})
    ).json()
    created_orgs.append(login["org_id"])
    headers = {"Authorization": f"Bearer {login['access_token']}"}

    conversation_id = (
        await client.post("/api/v1/conversations", json={"title": "Demo"}, headers=headers)
    ).json()["id"]
    message_id = (
        await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"content": "What are the processor's obligations?"},
            headers=headers,
        )
    ).json()["message_id"]
    ticket = (
        await client.post(
            "/api/v1/auth/stream-tickets", json={"message_id": message_id}, headers=headers
        )
    ).json()["ticket"]
    return conversation_id, message_id, ticket


def _stream_path(conversation_id: str, message_id: str, ticket: str) -> str:
    return (
        f"/api/v1/conversations/{conversation_id}/messages/{message_id}"
        f"/stream?ticket={ticket}"
    )


async def test_only_the_client_that_left_early_is_reported_as_cancelled(
    live_server, created_orgs: list[str]
) -> None:
    """A dropped connection logs `stream_cancelled_by_client` exactly once, and
    a completed one logs it never.

    Both halves are here rather than in two tests because the negative half
    otherwise needs a sleep to be worth anything. Run in this order it needs no
    sleep: the completed stream finishes first, so by the time the abandoned
    stream's line appears, the completed stream's disconnect handler has long
    since run and chosen to stay quiet. That handler *does* run on the happy
    path — uvicorn answers `http.disconnect` on the receive channel as soon as
    the response is complete — which is exactly why the quiet half needs
    proving.
    """
    base_url = f"http://127.0.0.1:{live_server.port}"

    async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
        finished_conversation, finished_message, finished_ticket = await _ask(
            client, created_orgs
        )
        url = _stream_path(finished_conversation, finished_message, finished_ticket)
        async with client.stream("GET", url) as response:
            body = "".join([chunk async for chunk in response.aiter_text()])
        assert "event: message_end" in body

        conversation_id, message_id, ticket = await _ask(client, created_orgs)

    # Raw socket, because httpx has no way to abandon a response mid-body
    # without also unwinding through its own cleanup.
    reader, writer = await asyncio.open_connection("127.0.0.1", live_server.port)
    writer.write(
        f"GET {_stream_path(conversation_id, message_id, ticket)} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{live_server.port}\r\n"
        "Accept: text/event-stream\r\n\r\n".encode()
    )
    await writer.drain()

    received = b""
    while received.count(b"event: token") < 2:
        chunk = await asyncio.wait_for(reader.read(4096), timeout=15)
        assert chunk, f"the server closed the stream instead of writing it: {received!r}"
        received += chunk
    assert b"event: message_end" not in received, "the answer finished before we could leave"

    # A closing tab, as far as the server can tell: FIN with the answer still
    # in flight.
    writer.close()
    await writer.wait_closed()

    assert len(live_server.wait_for("stream_cancelled_by_client", message_id)) == 1
    assert live_server.events("stream_cancelled_by_client", finished_message) == []
