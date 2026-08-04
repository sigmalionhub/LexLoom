import json
import uuid
from collections.abc import AsyncIterator

import structlog
from fastapi import APIRouter, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sse_starlette.sse import EventSourceResponse
from starlette.types import Message

from app.auth.deps import CurrentPrincipal
from app.auth.tickets import TICKET_TTL_SECONDS, mint_stream_ticket, verify_stream_ticket
from app.auth.verify import InvalidTokenError
from app.chat.prompt import build_grounded_prompt
from app.chat.providers import DEFAULT_GENERATOR, GeneratorChoice, build_provider
from app.chat.retrieval import SearchMode, search_chunks
from app.chat.stream import SSE_HEADERS, StreamProgress, client_close_handler, generate_events
from app.config import get_settings
from app.db.session import TenantSession, tenant_session
from app.errors import ProblemError
from app.limits import StreamSlot, acquire_stream_slot

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["chat"])

# The `status` column is a lifecycle, not a verdict: 'pending' means no final
# assistant row exists for this question yet, 'answered' means one does. The
# assistant row itself is written once and never appended to, so it is stored
# 'complete' whatever happened, and `finish_reason` — 'stop' or 'error' — says
# how generation ended. Keeping the two apart is what lets a reader tell "the
# provider failed and we said so" from "nobody ever answered this".
_PENDING = "pending"
_ANSWERED = "answered"
_COMPLETE = "complete"


class CreateConversationRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)


class CreateMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=8000)
    # Which `LLMProvider` should answer this question. Optional and defaulted
    # so every existing caller (and every earlier test) that posts only
    # `content` keeps working unchanged. Validated as the enum, not a bare
    # `str`: a caller who sends `"gemini-3.7-flash"` gets a `422` here, not a
    # 500 three requests later when the stream tries to build a provider for a
    # model that does not exist.
    generator: GeneratorChoice = DEFAULT_GENERATOR


class StreamTicketRequest(BaseModel):
    # Parsed as a UUID rather than kept as text so the audience string is built
    # from the canonical form on both sides. `str(uuid.UUID(...))` lower-cases
    # and normalises; without that, a client that sends the id upper-cased gets
    # a ticket whose audience never matches the one the stream endpoint derives
    # from its own path parameter, and the failure looks like a broken ticket.
    message_id: uuid.UUID


# Response models, declared as return annotations rather than `response_model=`
# — FastAPI reads either, and an annotation is the one a type checker reads too.
# Without them every 2xx in `openapi.json` is `{}`, `/docs` answers "an untyped
# object", and the generated client in `frontend/lib/api-types.ts` has nothing
# to offer but `Record<string, unknown>`.
class CreateConversationResponse(BaseModel):
    id: uuid.UUID


class CreateMessageResponse(BaseModel):
    message_id: uuid.UUID


class StreamTicketResponse(BaseModel):
    ticket: str
    # The ticket's real lifetime. A client times out on this, not on a literal.
    expires_in: int


@router.post("/conversations", status_code=status.HTTP_201_CREATED)
async def create_conversation(
    payload: CreateConversationRequest, principal: CurrentPrincipal, session: TenantSession
) -> CreateConversationResponse:
    conversation_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO conversations (id, org_id, title) VALUES (:i, :o, :t)"),
        {"i": str(conversation_id), "o": principal.org_id, "t": payload.title},
    )
    return CreateConversationResponse(id=conversation_id)


@router.post("/conversations/{conversation_id}/messages", status_code=status.HTTP_202_ACCEPTED)
async def create_message(
    conversation_id: uuid.UUID,
    payload: CreateMessageRequest,
    principal: CurrentPrincipal,
    session: TenantSession,
) -> CreateMessageResponse:
    """Accept the question and return immediately.

    Generation happens on the stream endpoint, not here. Answering 202 rather
    than holding the request open is what lets the client open the stream on
    its own terms — and it keeps this handler off the critical path of a slow
    provider.
    """
    # This schema has no foreign keys (they bypass RLS), so nothing but this
    # SELECT stops a message from being attached to a conversation that does not
    # exist. Cross-tenant writes are impossible either way — org_id is always
    # the caller's own and RLS holds the boundary — but the rows would be
    # unreachable through any conversation, and unreachable rows in a tenant
    # nobody can list are exactly what the purge cannot reason about. The lookup
    # runs under the caller's tenant context, so another organisation's
    # conversation is indistinguishable from a missing one.
    exists = await session.scalar(
        text("SELECT 1 FROM conversations WHERE id = :c"), {"c": str(conversation_id)}
    )
    if not exists:
        raise ProblemError(
            code="conversation_not_found",
            status=404,
            title="Conversation not found",
            detail="No such conversation.",
        )

    message_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO messages (id, org_id, conversation_id, role, content, status, generator) "
            "VALUES (:i, :o, :c, 'user', :content, :status, :generator)"
        ),
        {
            "i": str(message_id),
            "o": principal.org_id,
            "c": str(conversation_id),
            "content": payload.content,
            "status": _PENDING,
            # `.value`, not the `StrEnum` member itself: the driver binds this
            # as a parameter, and the plain string is what the column expects.
            "generator": payload.generator.value,
        },
    )
    return CreateMessageResponse(message_id=message_id)


@router.post("/auth/stream-tickets")
async def create_stream_ticket(
    payload: StreamTicketRequest, principal: CurrentPrincipal, request: Request
) -> StreamTicketResponse:
    """Mint the credential an EventSource can carry in its URL.

    The message id is not checked for existence here. A ticket is only ever
    honoured by the stream endpoint, which looks the message up in the ticket
    holder's own tenant context — so a ticket for someone else's message id, or
    for none at all, buys a 404 and nothing more. Checking here would add an
    existence oracle in exchange for nothing.
    """
    ticket = mint_stream_ticket(
        request.app.state.dev_idp, principal.subject, principal.org_id, str(payload.message_id)
    )
    # TICKET_TTL_SECONDS, not a second literal 60. Two numbers that must agree
    # is one number and a latent lie; the one in the contract is the one
    # clients build timeouts on.
    return StreamTicketResponse(ticket=ticket, expires_in=TICKET_TTL_SECONDS)


@router.get("/conversations/{conversation_id}/messages/{message_id}/stream")
async def stream_message(
    conversation_id: uuid.UUID, message_id: uuid.UUID, ticket: str, request: Request
) -> EventSourceResponse:
    try:
        principal = verify_stream_ticket(
            ticket,
            await request.app.state.jwks_cache.get(),
            get_settings().auth_issuer,
            str(message_id),
        )
    except InvalidTokenError as exc:
        # Without this the ticket-audience test gets a bare 500 from Starlette's
        # default error middleware instead of the 401 the contract promises.
        #
        # The detail is fixed and says nothing about WHY. The same oracle was
        # removed from the bearer-token path in commit bad23cc: a caller who
        # can tell "expired" from "wrong audience" from "unknown kid" can probe
        # for valid message ids and for the ticket's lifetime. The reason goes
        # to the log, where the operator can see it and the caller cannot.
        logger.info("stream_ticket_rejected", message_id=str(message_id), reason=str(exc))
        raise ProblemError(
            code="invalid_token",
            status=401,
            title="Invalid credentials",
            detail="The stream ticket is not valid for this message.",
        ) from exc

    # Both lookups run in one short transaction that closes before the first
    # token. Holding a session for the stream's lifetime exhausts the pool at
    # roughly fifteen concurrent streams, at which point readiness fails too.
    async with tenant_session(principal.org_id) as session:
        # `conversation_id` takes part in the query rather than decorating the
        # path. Without it a ticket would open the stream under any conversation
        # id the URL happened to carry, and `store_answer` — which reuses that
        # path parameter — would file the answer under a conversation that never
        # asked. `role` takes part too: it is what keeps a stored assistant row
        # from being addressable at all, rather than being found and refused by
        # the replay guard below with a 409 that confirms it exists.
        # test_a_ticket_does_not_open_its_message_under_a_different_conversation
        # and test_the_assistant_row_cannot_itself_be_streamed hold both clauses.
        message = (
            await session.execute(
                text(
                    "SELECT content, status, generator FROM messages "
                    "WHERE id = :i AND conversation_id = :c AND role = 'user'"
                ),
                {"i": str(message_id), "c": str(conversation_id)},
            )
        ).mappings().first()

        # 404 before the stream opens, not 200 with an error event inside:
        # an endpoint that answers 200 to every id is an existence oracle, and a
        # frontend cannot switch on an error it only learns about after
        # rendering a message bubble.
        if message is None:
            raise ProblemError(
                code="message_not_found",
                status=404,
                title="Message not found",
                detail="No such message in this conversation.",
            )
        # A ticket stays valid for its full minute, so the same URL can be
        # replayed — an EventSource reconnects on its own, and browsers retry
        # navigations. Refusing a question that already has its answer stored
        # keeps that from generating a second time and leaving two assistant
        # rows for one question. It is a replay guard, not a lock: two streams
        # opened concurrently before either has stored anything both proceed.
        if message["status"] != _PENDING:
            raise ProblemError(
                code="message_already_answered",
                status=409,
                title="Message already answered",
                detail="This message has been answered; fetch the conversation instead.",
            )

        # Order matters: the prompt is built FROM the citations, so retrieval
        # has to run first. Task 28 rev 1 built the prompt from the raw
        # question, which would have sent a visitor's question to Google under
        # the owner's key with no grounding in the corpus at all.
        #
        # `SearchMode.FTS` is written out rather than defaulted, and that is the
        # point of `mode` having no default: this line is *not yet* reading the
        # visitor's choice out of `messages.search_mode`, and the explicit
        # literal says so where a missing argument would have hidden it. Task 21
        # replaces it with `SearchMode(message["search_mode"])` — the column
        # already exists (migration 0005) and the row is already loaded here, so
        # that change is this one line and no new query.
        question = message["content"]
        citations = await search_chunks(session, question, mode=SearchMode.FTS)
        prompt = build_grounded_prompt(question, citations)
        # Read from the same row the question came from — no second query.
        # `GeneratorChoice(...)` re-validates rather than trusting the column:
        # the value was written by this same enum at POST time, but a value
        # written before a member was renamed, or poked in by hand, must fail
        # loudly here rather than reach `build_provider` as an opaque string.
        generator_choice = GeneratorChoice(message["generator"])

    # Built here, before the stream slot is reserved and before the response
    # exists, for two reasons at once. First, `GET /api/v1/generators` plus
    # the frontend disabling a Gemini option with no key is what keeps this
    # unreachable through the UI in the normal case (Task 30) — this is the
    # residual path for a caller that posted a Gemini choice directly (or
    # before a key was removed) and now opens the stream anyway. Second, once
    # `EventSourceResponse` exists the status code is already committed to
    # 200 and there is no way back to a problem+json body — so the refusal has
    # to happen before it, not inside `generate_events`. Before this, the
    # `ValueError` `build_provider` raises here was uncaught, propagated past
    # `create_app`'s only remaining handler and left as a generic `500
    # internal_error` that told the visitor nothing — and, built as an
    # argument to `generate_events(...)` after `acquire_stream_slot`, it also
    # leaked the slot it had already reserved.
    try:
        provider = build_provider(generator_choice)
    except ValueError as exc:
        logger.info(
            "generator_unavailable",
            message_id=str(message_id),
            generator=str(generator_choice),
        )
        raise ProblemError(
            code="generator_unavailable",
            status=422,
            title="Generator unavailable",
            detail=(
                f"{generator_choice} is not available on this server: no Gemini API "
                "key is configured. GET /api/v1/generators lists what is."
            ),
        ) from exc

    async def store_answer(answer: str, finish_reason: str) -> None:
        """Persist the answer in a fresh short transaction after generation.

        Runs inside the stream but not inside its transaction: the session is
        opened here, once, when there is finally something to write. It never
        runs on the cancellation path — see `app.chat.stream.generate_events` —
        so a question whose stream was abandoned keeps its 'pending' status,
        which is then the literal truth about it.
        """
        async with tenant_session(principal.org_id) as writer:
            await writer.execute(
                text(
                    "INSERT INTO messages (id, org_id, conversation_id, role, content, "
                    "status, finish_reason, citations) "
                    "VALUES (:i, :o, :c, 'assistant', :content, :status, :finish, "
                    "cast(:citations as jsonb))"
                ),
                {
                    "i": str(uuid.uuid4()),
                    "o": principal.org_id,
                    "c": str(conversation_id),
                    "content": answer,
                    "status": _COMPLETE,
                    "finish": finish_reason,
                    "citations": json.dumps(citations),
                },
            )
            await writer.execute(
                text("UPDATE messages SET status = :status WHERE id = :i"),
                {"status": _ANSWERED, "i": str(message_id)},
            )

    progress = StreamProgress()
    # Taken here, after every reason to refuse this request has been ruled out
    # and immediately before the response exists: a 404 or a 409 then costs no
    # slot, and nothing between the reservation and the response can raise and
    # strand one.
    slot = await acquire_stream_slot(principal.org_id)
    disconnected = client_close_handler(message_id=str(message_id), progress=progress)

    async def _release_on_disconnect(message: Message) -> None:
        await disconnected(message)
        await slot.release()

    return EventSourceResponse(
        _releasing(
            slot,
            generate_events(
                message_id=str(message_id),
                provider=provider,
                citations=citations,
                prompt=prompt,
                generator=generator_choice,
                progress=progress,
                on_complete=store_answer,
            ),
        ),
        headers=SSE_HEADERS,
        ping=15,  # keeps idle-timeout proxies from closing the connection
        client_close_handler_callable=_release_on_disconnect,
    )


async def _releasing(slot: StreamSlot, events: AsyncIterator[dict]) -> AsyncIterator[dict]:
    """Give the slot back when the stream ends, from both places it can end.

    This `finally` covers the ordinary ending and `aclose()`. It does not cover
    the one that matters most: `app.chat.stream` documents, from a measurement,
    that a client which goes away under backpressure leaves this frame
    suspended in a socket write and never resumed — so the `finally` may not
    run in observable time. A stream that ends that way would hold its slot
    until the TTL sweep, and sixteen abandoned streams would close the demo for
    ten minutes.

    That is why the disconnect path releases too, on the ASGI receive channel,
    which the same measurement showed is delivered regardless of where the
    response task is parked. Both paths run on a completed stream — the
    disconnect message arrives on the happy path as well — and `release` is a
    ZREM, which is idempotent.

    The `touch` is the other half of the same bookkeeping: nothing caps how
    long a stream may run, so a slot has to keep saying it is alive or the
    sweep will collect it while its connection is still open. It is throttled
    inside `StreamSlot`, so this call costs nothing on a short answer.
    """
    try:
        async for event in events:
            await slot.touch()
            yield event
    finally:
        await slot.release()
