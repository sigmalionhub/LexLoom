import json
import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from starlette.middleware.gzip import DEFAULT_EXCLUDED_CONTENT_TYPES, GZipMiddleware

from app.chat.stream import SSE_HEADERS
from app.db.session import tenant_session


async def test_stream_emits_events_in_the_contracted_order(client, demo_auth) -> None:
    conversation_id = await _create_conversation(client, demo_auth)
    message_id = (
        await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"content": "What are the processor's obligations?"},
            headers=demo_auth,
        )
    ).json()["message_id"]
    ticket = (
        await client.post("/api/v1/auth/stream-tickets", json={"message_id": message_id},
                          headers=demo_auth)
    ).json()["ticket"]

    events: list[str] = []
    url = f"/api/v1/conversations/{conversation_id}/messages/{message_id}/stream?ticket={ticket}"
    async with client.stream("GET", url) as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"
        async for line in response.aiter_lines():
            if line.startswith("event: "):
                events.append(line.removeprefix("event: ").strip())

    assert events[0] == "message_start"
    # Citations precede the first token: sources render immediately, so the
    # wait stops feeling empty. This is the fix for "responses appear all at
    # once after 20 seconds and it feels broken".
    assert events[1] == "citations"
    assert "token" in events
    assert events.index("citations") < events.index("token")
    assert events[-1] == "message_end"


async def test_stream_rejects_a_ticket_minted_for_another_message(client, demo_auth) -> None:
    """The ticket's audience is bound to one message id, so a leaked query
    string cannot be replayed against another stream."""
    conversation_id = await _create_conversation(client, demo_auth)
    first = (
        await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"content": "first"}, headers=demo_auth,
        )
    ).json()["message_id"]
    second = (
        await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"content": "second"}, headers=demo_auth,
        )
    ).json()["message_id"]
    ticket = (
        await client.post(
            "/api/v1/auth/stream-tickets", json={"message_id": first}, headers=demo_auth
        )
    ).json()["ticket"]

    response = await client.get(
        f"/api/v1/conversations/{conversation_id}/messages/{second}/stream?ticket={ticket}"
    )
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_token"


async def test_stream_response_is_not_compressed(client, demo_auth) -> None:
    """Compression over text/event-stream buffers the body and destroys
    incremental delivery, so no compression middleware may touch it."""
    conversation_id = await _create_conversation(client, demo_auth)
    message_id = (
        await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"content": "q"}, headers=demo_auth,
        )
    ).json()["message_id"]
    ticket = (
        await client.post(
            "/api/v1/auth/stream-tickets", json={"message_id": message_id}, headers=demo_auth
        )
    ).json()["ticket"]

    url = f"/api/v1/conversations/{conversation_id}/messages/{message_id}/stream?ticket={ticket}"
    async with client.stream("GET", url, headers={"Accept-Encoding": "gzip"}) as response:
        assert "content-encoding" not in response.headers


async def test_sse_headers_carries_only_what_the_library_does_not_set(client, demo_auth) -> None:
    """`SSE_HEADERS` omits both headers sse-starlette assigns for itself, and
    the wire carries them anyway.

    Both halves are asserted for each, because the comment in `SSE_HEADERS`
    justifies the omissions by claiming the second half: the library assigns
    them unconditionally after merging ours, so an entry there could never have
    changed a byte. If a future version stops doing that, the stream silently
    loses the nginx-buffering fix — so this test, not the comment, is what keeps
    that from happening quietly.
    """
    assert "Connection" not in SSE_HEADERS
    assert "X-Accel-Buffering" not in SSE_HEADERS

    conversation_id = await _create_conversation(client, demo_auth)
    message_id = await _ask(client, demo_auth, conversation_id, "q")
    ticket = await _ticket(client, demo_auth, message_id)

    url = _stream_url(conversation_id, message_id, ticket)
    async with client.stream("GET", url) as response:
        assert response.headers["connection"] == "keep-alive"
        assert response.headers["x-accel-buffering"] == "no"


class _WatchSend:
    """ASGI wrapper that runs a probe at the moment the response body goes out."""

    def __init__(self, app, probe) -> None:
        self._app = app
        self._probe = probe

    async def __call__(self, scope, receive, send) -> None:
        async def watched(message: dict) -> None:
            if message["type"] == "http.response.body" and not message.get("more_body"):
                await self._probe()
            await send(message)

        await self._app(scope, receive, watched)


async def test_a_write_is_committed_before_its_response_is_sent(
    app: FastAPI, demo_auth, demo_org_id
) -> None:
    """A 201 whose row is not committed yet is a promise the next request pays for.

    FastAPI closes a `yield` dependency's exit stack around the *request* by
    default — after `await response(scope, receive, send)` — so a session that
    commits on the way out commits after the client already has its 201. The
    client then posts a message on a conversation the database has not heard of
    and gets a 404 it cannot make sense of. In-process transports hide this
    completely, because they run the whole application call before handing back
    a response; it took a real socket and a real uvicorn to see it once.

    Asserted as ordering rather than as timing: the probe reads through a second
    session at the instant the body is sent, so this test is not a race it can
    win by being fast.
    """
    committed_when_sent: list[int] = []

    async def probe() -> None:
        async with tenant_session(demo_org_id) as session:
            committed_when_sent.append(
                await session.scalar(text("SELECT count(*) FROM conversations"))
            )

    transport = ASGITransport(app=_WatchSend(app, probe))
    async with AsyncClient(transport=transport, base_url="http://test") as probed:
        response = await probed.post(
            "/api/v1/conversations", json={"title": "Demo"}, headers=demo_auth
        )

    assert response.status_code == 201
    assert committed_when_sent == [1], (
        "the conversation was still uncommitted when its 201 went out, so a client that "
        "believes the response can be told the conversation does not exist"
    )


@pytest.fixture
def gzip_everything(app: FastAPI) -> None:
    """Put a compressor in front of the whole application.

    Must be requested before `client` in a test's signature: Starlette builds
    its middleware stack on the first request and refuses additions afterwards,
    and `demo_auth` makes a request.
    """
    # minimum_size=0 so nothing is skipped for being short, which would let the
    # test pass for a reason that has nothing to do with the stream.
    app.add_middleware(GZipMiddleware, minimum_size=0)


async def test_a_compressor_in_front_of_the_stream_cannot_touch_it(
    gzip_everything, client, demo_auth
) -> None:
    """The shipped application installs no compression middleware, so the test
    above passes whether or not the stream is protected from one. This is the
    same claim with the protection actually under load.

    What makes it hold is the content type: Starlette's GZipMiddleware skips
    `DEFAULT_EXCLUDED_CONTENT_TYPES`, and `text/event-stream` is in it. That is
    a property of this response, not of this application's middleware list, so
    it survives someone adding a compressor later — and it fails loudly here if
    the media type ever changes, which is the only way to lose it.
    """
    conversation_id = await _create_conversation(client, demo_auth)
    message_id = await _ask(client, demo_auth, conversation_id, "q")
    ticket = await _ticket(client, demo_auth, message_id)

    # The control: the compressor is live and does compress an ordinary
    # response from this very app. Without it the assertion below would hold
    # just as well over a middleware that was never installed.
    ordinary = await client.get("/openapi.json", headers={"Accept-Encoding": "gzip"})
    assert ordinary.headers["content-encoding"] == "gzip"

    url = _stream_url(conversation_id, message_id, ticket)
    async with client.stream("GET", url, headers={"Accept-Encoding": "gzip"}) as response:
        assert "content-encoding" not in response.headers
        assert response.headers["content-type"].startswith(DEFAULT_EXCLUDED_CONTENT_TYPES)


async def test_a_stream_ticket_is_not_accepted_as_a_bearer_token(client, demo_auth) -> None:
    """A ticket travels in a URL, so it lands in proxy logs, browser history and
    screenshots. Binding its audience to one message is what keeps that from
    being a credential for the rest of the API."""
    conversation_id = await _create_conversation(client, demo_auth)
    message_id = await _ask(client, demo_auth, conversation_id, "q")
    ticket = await _ticket(client, demo_auth, message_id)

    response = await client.post(
        "/api/v1/conversations",
        json={"title": "Opened with a ticket"},
        headers={"Authorization": f"Bearer {ticket}"},
    )
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_token"


async def test_a_message_cannot_be_created_on_a_conversation_that_does_not_exist(
    client, demo_auth
) -> None:
    """There are no foreign keys in this schema — they bypass RLS — so this
    check is the only thing standing between a typo and a message row no
    conversation can reach."""
    response = await client.post(
        f"/api/v1/conversations/{uuid.uuid4()}/messages",
        json={"content": "q"},
        headers=demo_auth,
    )
    assert response.status_code == 404
    assert response.json()["code"] == "conversation_not_found"


async def test_another_tenants_conversation_is_indistinguishable_from_a_missing_one(
    client, created_orgs
) -> None:
    theirs = await _demo_login(client, created_orgs)
    mine = await _demo_login(client, created_orgs)
    conversation_id = await _create_conversation(client, theirs)

    response = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": "q"},
        headers=mine,
    )
    assert response.status_code == 404
    assert response.json()["code"] == "conversation_not_found"


async def test_a_ticket_does_not_open_its_message_under_a_different_conversation(
    client, demo_auth
) -> None:
    """The conversation id in the stream URL is checked, not decoration.

    A ticket is bound to a message, not to a path, so without the
    `AND conversation_id = :c` clause any conversation id in the URL would open
    the stream — and `store_answer` reuses that path parameter, so the assistant
    row would be written under a conversation that never asked the question.
    That is the unreachable-row class the existence check on `create_message`
    exists to prevent, arriving through the write path instead.
    """
    asked_in = await _create_conversation(client, demo_auth)
    other = await _create_conversation(client, demo_auth)
    message_id = await _ask(client, demo_auth, asked_in, "q")
    ticket = await _ticket(client, demo_auth, message_id)

    response = await client.get(_stream_url(other, message_id, ticket))
    assert response.status_code == 404
    assert response.json()["code"] == "message_not_found"


async def test_another_tenants_conversation_id_does_not_open_a_stream(
    client, created_orgs
) -> None:
    """The same clause, crossed with the tenant boundary: a valid ticket for
    one's own message, presented under a conversation id belonging to somebody
    else, is refused — and refused with the same 404 as a missing one, so it
    does not report whether that conversation exists."""
    mine = await _demo_login(client, created_orgs)
    theirs = await _demo_login(client, created_orgs)
    my_conversation = await _create_conversation(client, mine)
    their_conversation = await _create_conversation(client, theirs)
    message_id = await _ask(client, mine, my_conversation, "q")
    ticket = await _ticket(client, mine, message_id)

    response = await client.get(_stream_url(their_conversation, message_id, ticket))
    assert response.status_code == 404
    assert response.json()["code"] == "message_not_found"


async def test_a_ticket_minted_by_another_tenant_finds_nothing_to_stream(
    client, created_orgs
) -> None:
    """Nothing stops a tenant minting a ticket for a message id it does not own
    — `create_stream_ticket` checks nothing, deliberately — so the boundary has
    to hold at the stream itself. It does: the ticket carries the minter's
    org_id, the lookup runs in that tenant's context, and another
    organisation's message is not there to be found."""
    mine = await _demo_login(client, created_orgs)
    theirs = await _demo_login(client, created_orgs)
    my_conversation = await _create_conversation(client, mine)
    my_message = await _ask(client, mine, my_conversation, "confidential question")

    their_ticket = await _ticket(client, theirs, my_message)

    response = await client.get(_stream_url(my_conversation, my_message, their_ticket))
    assert response.status_code == 404
    assert response.json()["code"] == "message_not_found"


async def test_the_assistant_row_cannot_itself_be_streamed(
    client, demo_auth, demo_org_id
) -> None:
    """`AND role = 'user'` keeps the stored answer from being addressable.

    Without it the assistant row is found and refused by the replay guard
    instead — a 409 where a 404 belongs, which tells the caller that the id it
    guessed is a real message in this conversation. Same tenant, so nothing
    crosses a boundary; it is still a distinction the endpoint has no reason to
    draw.
    """
    conversation_id = await _create_conversation(client, demo_auth)
    message_id = await _ask(client, demo_auth, conversation_id, "q")
    ticket = await _ticket(client, demo_auth, message_id)
    async with client.stream("GET", _stream_url(conversation_id, message_id, ticket)) as response:
        await _events(response)

    async with tenant_session(demo_org_id) as session:
        assistant_id = await session.scalar(
            text("SELECT id FROM messages WHERE conversation_id = :c AND role = 'assistant'"),
            {"c": conversation_id},
        )

    assistant_ticket = await _ticket(client, demo_auth, str(assistant_id))
    response = await client.get(_stream_url(conversation_id, assistant_id, assistant_ticket))
    assert response.status_code == 404
    assert response.json()["code"] == "message_not_found"


async def test_the_answer_is_stored_when_the_stream_finishes(
    client, demo_auth, demo_org_id
) -> None:
    """A reload has to show the answer, so the stream is not the only copy of it.

    Written after generation, in its own short transaction, and asserted here
    against what the client actually received rather than against the canned
    text — the two are the same answer or the feature is broken.

    The question is seeded to match a chunk on purpose. An empty `citations`
    column round-trips through JSONB whether or not the payload is built
    correctly; a real one is what proves the sources a reader saw are the
    sources a reload will show.

    It has to match *only* that chunk, which is why the question is a phrase
    from a commercial contract rather than a word of regulation: every demo
    tenant is provisioned owning a copy of the public GDPR corpus, so a question
    like "obligations" now finds that too and the comparison below stops being
    about one known row.
    """
    async with tenant_session(demo_org_id) as session:
        await session.execute(
            text(
                "INSERT INTO document_chunks (id, org_id, content, page_from, page_to) "
                "VALUES (:i, :o, :c, 3, 4)"
            ),
            {
                "i": str(uuid.uuid4()),
                "o": demo_org_id,
                "c": "The obligations of the processor survive termination.",
            },
        )

    conversation_id = await _create_conversation(client, demo_auth)
    message_id = await _ask(client, demo_auth, conversation_id, "survive termination")
    ticket = await _ticket(client, demo_auth, message_id)

    async with client.stream("GET", _stream_url(conversation_id, message_id, ticket)) as response:
        events = await _events(response)
    spoken = "".join(data["text"] for name, data in events if name == "token")
    streamed_chunks = next(data["chunks"] for name, data in events if name == "citations")

    async with tenant_session(demo_org_id) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT role, content, status, finish_reason, citations FROM messages "
                    "WHERE conversation_id = :c ORDER BY role"
                ),
                {"c": conversation_id},
            )
        ).mappings().all()

    assert [row["role"] for row in rows] == ["assistant", "user"]
    answer, question = rows
    assert answer["content"] == spoken
    assert answer["status"] == "complete"
    assert answer["finish_reason"] == "stop"
    # The stored sources are the streamed sources, field for field, after a
    # round trip through JSONB — not merely a list of the right length.
    assert answer["citations"] == streamed_chunks
    assert len(streamed_chunks) == 1, (
        "the question was meant to match only the chunk this test seeded; if the public "
        "corpus every demo tenant now owns contains this phrase too, change the phrase "
        "rather than the assertion — the comparison above is what makes this test mean "
        "anything"
    )
    assert streamed_chunks[0].keys() == {
        "chunk_id", "org_id", "content", "page_from", "page_to", "score"
    }
    assert streamed_chunks[0]["org_id"] == demo_org_id
    assert streamed_chunks[0]["page_from"] == 3
    assert streamed_chunks[0]["page_to"] == 4
    assert streamed_chunks[0]["score"] == 1.0
    assert "survive termination" in streamed_chunks[0]["content"]
    uuid.UUID(streamed_chunks[0]["chunk_id"])  # a real id, not a repr
    # 'pending' is what a question keeps when nobody stayed for the answer, so
    # it has to move once one exists — otherwise the column says nothing.
    assert question["status"] == "answered"


async def test_replaying_a_finished_stream_is_refused(client, demo_auth) -> None:
    """The ticket stays valid for its full minute and an EventSource reconnects
    on its own, so the same URL arrives twice. Generating twice would bill twice
    and leave two assistant rows for one question."""
    conversation_id = await _create_conversation(client, demo_auth)
    message_id = await _ask(client, demo_auth, conversation_id, "q")
    ticket = await _ticket(client, demo_auth, message_id)
    url = _stream_url(conversation_id, message_id, ticket)

    async with client.stream("GET", url) as response:
        await _events(response)

    replay = await client.get(url)
    assert replay.status_code == 409
    assert replay.json()["code"] == "message_already_answered"


async def test_citations_carry_the_tenants_own_chunks(client, demo_auth, demo_org_id) -> None:
    """The citations event is the whole point of the ordering: it is what the
    user reads while the answer is still arriving."""
    async with tenant_session(demo_org_id) as session:
        await session.execute(
            text(
                "INSERT INTO document_chunks (id, org_id, content, page_from, page_to) "
                "VALUES (:i, :o, :c, 7, 7)"
            ),
            {
                "i": str(uuid.uuid4()),
                "o": demo_org_id,
                "c": "The processor's obligations include a 72-hour breach notice.",
            },
        )

    conversation_id = await _create_conversation(client, demo_auth)
    # M1 retrieval is a literal substring match, so the question has to be one.
    # M2 replaces the body of search_chunks with hybrid search and this stops
    # being a constraint on what can be asked.
    message_id = await _ask(client, demo_auth, conversation_id, "processor's obligations")
    ticket = await _ticket(client, demo_auth, message_id)

    async with client.stream("GET", _stream_url(conversation_id, message_id, ticket)) as response:
        events = await _events(response)

    chunks = next(data["chunks"] for name, data in events if name == "citations")
    assert len(chunks) == 1, (
        "the question was meant to match only the chunk this test seeded; if the public "
        "corpus every demo tenant now owns contains this phrase too, change the phrase"
    )
    assert chunks[0]["org_id"] == demo_org_id
    assert chunks[0]["page_from"] == 7
    assert "72-hour breach notice" in chunks[0]["content"]


async def test_a_visitor_who_has_uploaded_nothing_still_gets_sources(
    client, demo_auth, demo_org_id
) -> None:
    """The demo's acceptance criterion, over the wire, for the visitor it is about.

    Every other test in this file seeds a chunk first, so all of them would stay
    green with demo tenants provisioned empty — which is exactly what happened.
    Nothing raises when a tenant owns no chunks: `search_chunks` returns `[]`,
    the `citations` event goes out with an empty list, the ordering assertions
    above still hold, and the demo shows no sources at all. Only asking as a
    visitor who has uploaded nothing catches that.
    """
    conversation_id = await _create_conversation(client, demo_auth)
    # A word, not a sentence: M1 retrieval is a literal substring match.
    message_id = await _ask(client, demo_auth, conversation_id, "processor")
    ticket = await _ticket(client, demo_auth, message_id)

    async with client.stream("GET", _stream_url(conversation_id, message_id, ticket)) as response:
        events = await _events(response)

    names = [name for name, _ in events]
    chunks = next(data["chunks"] for name, data in events if name == "citations")
    assert chunks, "a visitor who has uploaded nothing is shown no sources"
    assert names.index("citations") < names.index("token")
    assert {chunk["org_id"] for chunk in chunks} == {demo_org_id}


async def _create_conversation(client, headers) -> str:
    response = await client.post("/api/v1/conversations", json={"title": "Demo"}, headers=headers)
    return response.json()["id"]


async def _demo_login(client: AsyncClient, created_orgs: list[str]) -> dict[str, str]:
    """A second tenant in the same test, registered for the same teardown."""
    body = (await client.post("/api/v1/auth/demo-login", json={"label": "Globex"})).json()
    created_orgs.append(body["org_id"])
    return {"Authorization": f"Bearer {body['access_token']}"}


async def _ask(client, headers, conversation_id: str, content: str) -> str:
    response = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": content},
        headers=headers,
    )
    return response.json()["message_id"]


async def _ticket(client, headers, message_id: str) -> str:
    response = await client.post(
        "/api/v1/auth/stream-tickets", json={"message_id": message_id}, headers=headers
    )
    return response.json()["ticket"]


def _stream_url(conversation_id: str, message_id: str, ticket: str) -> str:
    return (
        f"/api/v1/conversations/{conversation_id}/messages/{message_id}/stream?ticket={ticket}"
    )


async def _events(response) -> list[tuple[str, dict]]:
    """Pair each `event:` line with the `data:` line that follows it."""
    pairs: list[tuple[str, dict]] = []
    name = ""
    async for line in response.aiter_lines():
        if line.startswith("event: "):
            name = line.removeprefix("event: ").strip()
        elif line.startswith("data: "):
            pairs.append((name, json.loads(line.removeprefix("data: "))))
    return pairs
