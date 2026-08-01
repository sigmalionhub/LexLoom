# ADR 0002 — Server-Sent Events for streaming, not WebSocket

**Status:** accepted
**Date:** 2026-08-01

## Context

The complaint this project was built to answer is specific: *responses appear all at
once after twenty-odd seconds and it feels broken.* The fix is not a faster model. It
is showing the user something within a few hundred milliseconds and then filling in
the answer as it arrives.

That means a server-to-client push. The candidates were WebSocket, Server-Sent
Events, and a chunked `fetch` with a hand-rolled framing.

WebSocket is the default answer, and for this problem it is the wrong one. The data
flows in exactly one direction. The client sends a question over an ordinary `POST`
and then listens. Nothing travels upstream over the live connection at all — so a
bidirectional protocol buys a second transport, a second authentication path, a second
error vocabulary, and a second thing to keep working behind a reverse proxy, in
exchange for a capability the feature does not use.

## Decision

**Server-Sent Events over ordinary HTTP,** with named events, delivered by
`sse-starlette`'s `EventSourceResponse`.

Concretely, what SSE buys here is that a stream is *just a response*:

- **The same error contract.** Everything that can fail before the first byte — bad
  ticket, unknown message, already-answered message — fails as an HTTP status with an
  RFC 9457 body, identical in shape to every other error in the API. A WebSocket
  upgrade that fails has no such vocabulary; you get a close code and you invent a
  convention. See [errors.md](../errors.md).
- **The same middleware.** Request-id correlation, structured logging, the catch-all
  handler. One stack, not two.
- **The same proxy configuration.** The nginx vhost that fronts the API fronts the
  stream. No `Upgrade` handling, no separate timeout policy.
- **The same authentication primitive.** An RS256 JWT verified against a JWKS, with
  one adaptation described below.
- **It degrades to `curl`.** Everything in [api-contract.md](../api-contract.md) is a
  `curl` invocation. A frontend developer debugging a stream does not need a special
  client.

The event vocabulary is deliberately named rather than a single `message` channel
carrying a discriminated union, so a browser consumer attaches one listener per
concern and never writes a `switch` on a `type` field it has to keep in sync.

### The consequence that is not free: `EventSource` cannot send a header

The browser `EventSource` API is GET-only and has no way to set `Authorization`. That
is the real cost of this decision and it shapes two endpoints.

The chat POST and the stream are therefore **separate endpoints**: `POST …/messages`
returns `202 { message_id }` carrying a normal bearer token, and the stream is a `GET`
authenticated by a **60-second, single-message ticket** in the query string, with
`aud: "stream:<message_id>"`. A ticket is not accepted anywhere else, and a bearer
token is not accepted as a ticket — the audiences differ, in both directions.

The residual is stated rather than hidden: **the ticket is written to the access log,
because it is in the URL.**

```
"GET /api/v1/conversations/…/messages/…/stream?ticket=eyJhbGciOiJSUzI1NiIsImtpZCI6…" 200 OK
```

It is live at the moment the line is written. Sixty seconds is what bounds that, along
with the fact that a ticket cannot mint another ticket and the stream endpoint is the
only one that honours it. That is a real reduction, not an elimination: within its
minute, a ticket lifted from a log is a valid credential for one message — and since
the stream endpoint *writes* the answer, it is a write credential, and its `citations`
event carries up to five 400-character excerpts of that tenant's own corpus, selected
by that tenant's question. A `POST`-based streaming endpoint with a header would not
have this property, and a frontend that hand-rolls `fetch` + `ReadableStream` could
use one. Keeping `EventSource` viable was judged worth it for a demo whose whole point
is that a frontend developer can consume it in five lines.

### Things that had to be measured, not assumed

- **`X-Accel-Buffering: no`.** Without it nginx buffers the whole response and the
  stream arrives as one lump — the exact symptom the feature exists to remove, but
  only in production. The header is set by `sse-starlette` itself.
- **No compression, ever, in front of this.** gzip over SSE destroys incremental
  delivery and is exactly the middleware someone adds later "for performance".
- **`Cache-Control: no-cache`,** overriding the library's `no-store`. An SSE body is a
  live connection, not a document; revalidation is the meaningful instruction.
- **A `ping` every 15 s** for idle-timeout proxies. With the bundled provider a stream
  finishes in about 1.3 s, so in practice no ping frame is ever emitted — measured,
  zero comment frames across a full stream. It exists for a slower real provider.
- **Request-context middleware is raw ASGI, not `BaseHTTPMiddleware`.**
  `BaseHTTPMiddleware` wraps the receive channel, and that breaks client-disconnect
  propagation into a streaming response. In a service whose headline feature is a
  cancellable stream, adding request context the ordinary way silently disables
  cancellation.
- **No database session is held across a stream.** Retrieval runs in a short
  transaction that closes before the first token; the answer is written in a fresh one
  afterwards. Holding a session open for a stream's lifetime would exhaust the pool at
  roughly fifteen concurrent streams — at which point `/health/ready` fails too and the
  supervisor restarts into a loop. Measured instead: 30 concurrent streams completed in
  2.78 s, all `200`, all 41 tokens, with `/health/ready` still answering `200`.

Versions this was measured against: FastAPI 0.141.1, Starlette 1.3.1, uvicorn 0.52.0,
`sse-starlette` 3.4.6.

## Consequences

- No `Last-Event-ID` resumption: frames carry no `id:`. A dropped stream is re-opened
  from the beginning, which is safe because a dropped stream stores nothing.
- `EventSource` reconnects automatically when a stream ends — *including when it ends
  successfully*. That made the `409 message_already_answered` guard load-bearing for
  ordinary correctness, not only for replay: without it, every completed answer would
  be regenerated about three seconds later. Measured, with the client deliberately not
  closed:

  ```
  message_start #1 at t+16ms
  message_end   #1 at t+1280ms
  error/close at t+1289ms (readyState=0)   <- CONNECTING: retrying
  error/close at t+4305ms (readyState=2)   <- CLOSED: the 409 stopped it
  ```

  ```
  "GET …/stream" 200 OK
  "GET …/stream" 409 Conflict
  ```

- The API has no CORS middleware at all, so the frontend must be same-origin behind
  the reverse proxy. That is a consequence of the SSE choice only indirectly, but it
  is the coupling a reader will hit first.
- If bidirectional traffic ever becomes real — interrupt-and-redirect mid-answer,
  collaborative editing — this decision should be revisited rather than extended.
  Tunnelling upstream messages through side-channel `POST`s to keep SSE would be the
  wrong repair.

---

## What I got wrong and what it cost

### The response was sent before its own transaction committed

This is the worst bug in the project, it was on the critical path, and every
in-process test passed through it.

The tenant-scoped database session is a FastAPI `yield` dependency. FastAPI's default
closes a `yield` dependency's exit stack around the whole **request** — in
`fastapi/routing.py`, the dependency stack unwinds after `await response(scope,
receive, send)`, not after the handler returns. So `POST /conversations` could hand a
`201` and an id to the client *before the row was committed*, and the next request —
`POST …/messages` against that id — could be answered `404`. A `202 Accepted` could be
followed by a stream that said the message did not exist.

It is invisible to `httpx`'s `ASGITransport`, which runs the entire application call
before returning a response, so the ordering the bug depends on never occurs. It
surfaced once as an unexplained intermittent failure against a real uvicorn and was
nearly written off as flakiness.

The fix is one keyword:

```python
TenantSession = Annotated[AsyncSession, Depends(get_tenant_session, scope="function")]
```

`"function"` ends the dependency after the handler and before the response goes out,
which is what a `2xx` is normally taken to mean. There is now a test that probes the
database at the instant the body is sent.

What it cost: a shared module every later endpoint depends on had to change late, and
the isolation properties built on top of it — that `set_config` does not outlive its
transaction, that the structural guard still fires, that a handler which writes and
then raises commits nothing — all had to be re-verified against the new lifetime.
More lastingly, it is the clearest case in this build of a whole test suite agreeing
with a bug, because the transport under test could not express the failure. A
green suite over the wrong transport is not evidence.

### Cancellation was logged in the place where it cannot be observed

The obvious way to notice a client hanging up mid-answer is to catch
`asyncio.CancelledError` around the generator. It is what the design said, and it does
not work.

Three separate reasons, all found by measurement on this stack:

1. A disconnect cancels the task driving the response **wherever it happens to be
   suspended**. A client that goes away under backpressure leaves it suspended in the
   socket write, not in the generator — so the generator's `except` never runs.
2. A tidy consumer calling `aclose()` throws `GeneratorExit`, which is a *sibling* of
   `CancelledError` under `BaseException`. `except asyncio.CancelledError` does not
   catch it.
3. A generator abandoned by `break` without closing is never finalised in observable
   time.

Generation stops in every one of those cases. Only the bookkeeping is
timing-dependent — which is worse, because it means the log agrees that nothing
happened.

The log line now lives on the ASGI `http.disconnect` message, which the server
delivers on the receive channel regardless of where the response task is parked, and
it is verified by dropping a raw socket mid-answer against a real uvicorn:

```console
$ curl -sN --max-time 1 ".../stream?ticket=$TICKET" > partial.txt
curl: (28) Operation timed out after 1000 milliseconds with 4081 bytes received
```
```json
{"message_id": "6c58f611-00e1-46d0-b5c6-40f3e5c65893", "event": "stream_cancelled_by_client", …}
```

A completion flag is load-bearing there, because uvicorn delivers `http.disconnect` on
the happy path too — without it every successful stream would log a cancellation and
the line would mean nothing.

Cost: the acceptance criterion for this milestone was *"close the tab mid-generation
and `stream_cancelled_by_client` appears in the logs"*, checked by a human in a
browser. The implementation that the design called for would have passed a unit test
that cancels a task, and failed the browser, silently.

### A test that could not fail, and a header that could not do anything

Two smaller ones, same shape: an artefact that reads as protection and protects
nothing.

`test_stream_response_is_not_compressed` asserts that a compression middleware does
not gzip the stream. Installing `GZipMiddleware` app-wide at `minimum_size=0` left
every test green — because Starlette 1.3.1 ships
`DEFAULT_EXCLUDED_CONTENT_TYPES = ("text/event-stream",)`. The property is real and
important; the *test* guards nothing, because the framework already guarantees it and
nothing in this repository can break it. That is a category worth naming: a test can
fail to be falsifiable not because it is badly written but because the thing it
asserts is not ours to break. The honest options are to say so or to delete it.

`SSE_HEADERS` used to carry `X-Accel-Buffering: no` and `Connection: keep-alive`.
`sse-starlette` assigns both unconditionally *after* merging the application's
headers, so neither entry could ever change a byte on the wire — while the comment
beside them called the first "the nginx-buffering fix the endpoint exists for".
Deleting them changed nothing observable, which is the proof. `SSE_HEADERS` is now one
entry, `Cache-Control`, which is the only one the library applies with `setdefault`
and therefore the only one the application actually decides.

Cost of both: near zero in behaviour, and real in credibility. A reader who checks
either claim finds it false, and then has to re-check the claims that matter.
