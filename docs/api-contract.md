# LexLoom API contract

This is the document a frontend developer reads before writing the first `fetch`.
It describes **only what the service currently implements**. Every request in it was
run against the stack in `docker-compose.yml` and every response was pasted back,
including the header blocks and the timings. Nothing here was written from memory,
and nothing is aspirational — if a header, an event or an error code is not in this
file, it does not exist yet.

Base URL in development: `http://127.0.0.1:8100`.

---

## 1. The whole surface

Eight routes. That is the entire API.

| Method | Path | Auth | Success |
|---|---|---|---|
| `GET` | `/health/live` | none | `200` |
| `GET` | `/health/ready` | none | `200` / `503` |
| `GET` | `/.well-known/jwks.json` | none | `200` |
| `POST` | `/api/v1/auth/demo-login` | none | `200` |
| `POST` | `/api/v1/conversations` | bearer | `201` |
| `POST` | `/api/v1/conversations/{conversation_id}/messages` | bearer | `202` |
| `POST` | `/api/v1/auth/stream-tickets` | bearer | `200` |
| `GET` | `/api/v1/conversations/{cid}/messages/{mid}/stream` | ticket | `200` (SSE) |

`GET /openapi.json` and the Swagger UI at `/docs` are also served, unauthenticated.

Two of these routes can refuse before they do any work: `POST /api/v1/auth/demo-login`
is metered per client address and answers `429`, and the stream endpoint has a
concurrency ceiling and answers `503`. Both carry `Retry-After` —
see [§6.1](#61-refusals-429-503-and-retry-after).

**There are no read endpoints.** No conversation list, no message list, no
`GET /conversations/{id}`. A client holds the state it created, or it loses it on
reload. This is a real limitation, not an omission from the document — see
[§9](#9-what-this-api-does-not-have).

---

## 2. Authentication

Two credentials, and they are not interchangeable.

**The bearer token** is an RS256 JWT verified against a JWKS. In development the
JWKS is served by this process; in production it is meant to be Clerk's, and
`AUTH_JWKS_URL` is the only setting that differs. Verification pins the algorithm
to `RS256` from configuration (never from the token header), selects the key by
`kid`, and requires `exp`, `iss`, `aud`, `sub` plus a non-empty `org_id`.

```console
$ curl -s http://127.0.0.1:8100/.well-known/jwks.json | jq '.keys[0] | keys'
["alg","e","kid","kty","n","use"]
```

The signing keypair is generated in memory at process start and is never written to
disk, so **the `kid` changes on every restart and all previously issued tokens stop
verifying**. Measured across one `docker compose restart api`:

```
kid before: 51186bde67b44899843645a1858e0338    modulus: y1oKvJCwRVSkCc2xsFyjDlng…
kid after : 71f3d94d5c9f4f5a810fff66903fa37f    modulus: nAkKDEZpsGitAuVbTOARctU8…
a token minted before the restart -> HTTP 401 invalid_token
```

(The modulus is compared, not only the `kid` — a `kid` is a random identifier and
would change on its own even if the key were reused.) This is deliberate for a public
demo: there is no key material in the repository to leak. It is also the thing most
likely to surprise you if you keep a token in `localStorage` across a redeploy.

Claims of a token issued by `POST /api/v1/auth/demo-login`:

```json
{
  "iss": "https://lexloom.dev-idp",
  "aud": "lexloom-api",
  "sub": "demo_268bb2a143af4a05a9ae9638f5b7fe6f",
  "org_id": "268bb2a1-43af-4a05-a9ae-9638f5b7fe6f",
  "iat": 1785549354,
  "nbf": 1785549354,
  "exp": 1785552954
}
```

Lifetime 3600 s. `org_id` is the tenant identity: it is the only thing the API ever
uses to decide what a caller can see, and it is read from the verified token — never
from a header, a query parameter or a body field.

**The stream ticket** is a second RS256 JWT with `aud: "stream:<message_id>"` and a
60-second lifetime. It exists because `EventSource` is GET-only and cannot set an
`Authorization` header. It is accepted by exactly one endpoint, for exactly one
message. Presenting a ticket as a bearer token, or a bearer token as a ticket, fails
audience verification in both directions.

---

## 3. The chat flow, end to end

Four calls. Below is one continuous session, captured in order.

### 3.1 Get a tenant

`POST /api/v1/auth/demo-login` provisions a throwaway organisation, copies the seed
corpus into it, and mints a token for it. It needs no credentials — that is the
point of the demo — and it is the only unauthenticated write in the API.

```console
$ curl -s -X POST http://127.0.0.1:8100/api/v1/auth/demo-login \
    -H 'Content-Type: application/json' \
    -d '{"label":"Acme Legal"}'
```
```json
{
  "access_token": "eyJhbGciOiJSUzI1NiIsImtpZCI6IjUxMTg2YmRlNjdiNDQ4OTk4NDM2NDVhMTg1OGUwMzM4IiwidHlwIjoiSldUIn0…",
  "org_id": "268bb2a1-43af-4a05-a9ae-9638f5b7fe6f",
  "org_name": "Acme Legal",
  "expires_in": 3600
}
```

`label` is `1..64` characters after whitespace stripping, and must contain at least
one character that renders — a label made entirely of zero-width or filler
codepoints is a `422`, because an organisation whose name draws nothing is
indistinguishable in a UI from one whose name failed to load.

The tenant is reclaimed 24 hours after creation, along with everything in it.

### 3.2 Create a conversation

```console
$ curl -s -X POST http://127.0.0.1:8100/api/v1/conversations \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d '{"title":"Breach notification"}' -i
```
```http
HTTP/1.1 201 Created
content-type: application/json
x-request-id: 2ecdd74d-3bd7-4806-b167-64dbcd2383ee

{"id":"1ae24e40-1ab4-4c02-9130-90bbb75be70d"}
```

`title` is `1..255` characters. The `201` means the row is committed: the dependency
that owns the transaction is closed before the response is written, not after it, so
the id in this body is usable on the very next request. (It was not always. See
[ADR 0002 §"What I got wrong"](adr/0002-sse-over-websocket.md#what-i-got-wrong-and-what-it-cost).)

### 3.3 Ask the question

```console
$ curl -s -X POST http://127.0.0.1:8100/api/v1/conversations/1ae24e40-1ab4-4c02-9130-90bbb75be70d/messages \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d '{"content":"How long do we have to report a breach?"}' -i
```
```http
HTTP/1.1 202 Accepted
content-type: application/json
x-request-id: 472f68ef-e44a-4af9-9aea-eef6a1d8dd48

{"message_id":"d2f0ab9e-86f0-4e11-bd11-2406578dfa2d"}
```

`content` is `1..8000` characters. `202`, not `201`: the question is stored, nothing
has been generated, and the answer arrives over the stream. The message is stored
with `status: "pending"` and stays that way until a stream completes for it.

### 3.4 Mint a ticket and open the stream

```console
$ curl -s -X POST http://127.0.0.1:8100/api/v1/auth/stream-tickets \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d '{"message_id":"d2f0ab9e-86f0-4e11-bd11-2406578dfa2d"}'
```
```json
{"ticket": "eyJhbGciOiJSUzI1NiIsImtp…", "expires_in": 60}
```

`expires_in` is the ticket's real lifetime, read from the same constant the ticket
was minted with. Build your timeout on it rather than on 60.

This endpoint does not check that the message exists. It cannot usefully: the stream
endpoint looks the message up in the ticket holder's own tenant, so a ticket for
someone else's message id — or for no message at all — buys a `404` and nothing more.
Checking here would add an existence oracle in exchange for nothing.

```console
$ curl -sN "http://127.0.0.1:8100/api/v1/conversations/1ae24e40-1ab4-4c02-9130-90bbb75be70d/messages/d2f0ab9e-86f0-4e11-bd11-2406578dfa2d/stream?ticket=$TICKET" -D -
```
```http
HTTP/1.1 200 OK
cache-control: no-cache
connection: keep-alive
x-accel-buffering: no
content-type: text/event-stream; charset=utf-8
x-request-id: 6528b81b-9c32-4d13-b652-1f7b953d17a8
transfer-encoding: chunked
```
```
first_byte=0.018053s  total=1.289160s  bytes=3399  frames=44
```

First byte at 18 ms; the whole answer in 1.29 s across 44 frames. Retrieval happens
before the response starts, so the `citations` frame is already on the wire when the
UI paints.

---

## 4. The SSE contract

### 4.1 Framing

Frames are separated by a **blank line**, and this service uses **CRLF** line
endings, not LF:

```
$ xxd stream.body | head
00000000: 6576 656e 743a 206d 6573 7361 6765 5f73  event: message_s
00000010: 7461 7274 0d0a 6461 7461 3a20 7b22 6d65  tart..data: {"me
…                                                  (message_id, model)
00000060: 652d 3122 7d0d 0a0d 0a65 7665 6e74 3a20  e-1"}....event:
```

`0d0a` after `message_start`, and `0d0a0d0a` between frames.

If you hand-roll a parser, split on `\r\n\r\n` or normalise `\r\n` → `\n` first.
Splitting on `\n\n` yields exactly zero frames and no error — which is how this
particular sentence came to be written.

`data` is always a single line of JSON. There are no multi-line `data:` payloads and
no `id:` fields, so this stream does not support `Last-Event-ID` resumption.

### 4.2 Events

| `event` | `data` | When |
|---|---|---|
| `message_start` | `{"message_id": "<uuid>", "model": "fake-1"}` | Always first. |
| `citations` | `{"chunks": [Citation, …]}` | Always second, **before any token**. May be `[]`. |
| `token` | `{"text": "Under "}` | Zero or more. Concatenate `text` in order; whitespace is included in the token. |
| `error` | `{"code": "provider_unavailable", "message": "…"}` | At most once, if generation fails part-way. |
| `message_end` | `{"finish_reason": "stop" \| "error"}` | Always last, on both the success and the failure path. |

A `Citation`:

```json
{
  "chunk_id": "cf0d6e41-49c7-42df-9389-2242b6602de2",
  "org_id": "268bb2a1-43af-4a05-a9ae-9638f5b7fe6f",
  "content": "Article 33(1) GDPR — In the case of a personal data breach, the controller shall without undue delay and, where feasible, not later than 72 hours after having become aware of it, notify the personal data breach to the supervisory authority…",
  "page_from": 52,
  "page_to": 52,
  "score": 0.027581878006458282
}
```

At most five chunks, ranked. `content` is truncated at 400 characters. `score` is a
raw Postgres `ts_rank` value: **comparable within one response and nowhere else.**
It is not normalised to `0..1`, so do not render it as a percentage and do not
threshold on it across questions.

### 4.3 Ordering guarantees

1. `message_start` first, `message_end` last.
2. `citations` arrives before the first `token`, always. Retrieval finishes before
   generation begins, so the sources render while the answer is still typing. This
   is the whole reason the stream is shaped this way.
3. `error` and `message_end` both arrive; an `error` does not truncate the stream.
   The HTTP status is already `200` by then, so a failure mid-generation is reported
   *inside* the stream, and `finish_reason` is `"error"`.
4. Everything that can fail before the first byte — bad ticket, unknown message,
   already-answered message — fails as an HTTP status with a problem+json body.
   The stream never opens `200` just to report a `404` in an event.

The `error` branch is the one part of this table that **cannot be produced by curl
against the shipped build**: the bundled provider is deterministic and does not fail.
Driving the shipped generator with a provider that fails mid-answer gives the real
frames —

```
event: token
data: {"text": "Under "}

event: error
data: {"code": "provider_unavailable", "message": "Generation failed; sources are above."}

event: message_end
data: {"finish_reason": "error"}
```

— and the partial answer produced before the failure is stored rather than discarded,
with `finish_reason: "error"` recording how it ended. Note that the `error` event says
what went wrong in terms you can switch on and nothing about the exception: the
provider's own message goes to the log, never onto the wire.

The whole branch — the two `code`/`message` members, `finish_reason: "error"`, the
partial answer being kept, and the exception text *not* appearing — is asserted by
`test_a_failure_mid_answer_keeps_the_tokens_and_names_the_reason`. So this row is
test-verified rather than curl-verified, which is the only part of the contract where
those differ.

### 4.4 Headers

| Header | Value | Why |
|---|---|---|
| `content-type` | `text/event-stream; charset=utf-8` | |
| `cache-control` | `no-cache` | Set by this application, overriding the library's `no-store`. |
| `x-accel-buffering` | `no` | Stops nginx buffering the whole response. Without it the stream arrives as one lump at the end. |
| `connection` | `keep-alive` | Set by the SSE library. Hop-by-hop, so a proxy rewrites it. |
| `transfer-encoding` | `chunked` | |

**Never put a compression middleware in front of this.** gzip over SSE destroys
incremental delivery: it is exactly the thing added later "for performance" that
turns a working stream into a 20-second wait.

A keepalive comment (`: ping`) is configured at 15 s. With the shipped provider a
stream completes in about 1.3 s, so in practice **no ping frame is ever emitted** —
measured, zero comment frames in a full stream. It matters for a slower real
provider; parse `:`-prefixed lines as comments and ignore them.

---

## 5. Consuming the stream

### 5.1 `EventSource` — the browser path

Both snippets below were executed against the running service.

```js
const url = `${API}/api/v1/conversations/${conversationId}/messages/${messageId}/stream`
          + `?ticket=${encodeURIComponent(ticket)}`;
const es = new EventSource(url);
let answer = "";

es.addEventListener("message_start", (e) => console.log("start", JSON.parse(e.data)));
es.addEventListener("citations", (e) => renderSources(JSON.parse(e.data).chunks));
es.addEventListener("token", (e) => { answer += JSON.parse(e.data).text; render(answer); });
es.addEventListener("message_end", (e) => {
  console.log("end", JSON.parse(e.data));
  es.close();                       // <- REQUIRED, see below
});
es.onerror = () => { es.close(); }; // transport-level, NOT the server's `error` event
```

```
message_start { message_id: '668133ee-9255-4330-916c-077f9ef80a9c', model: 'fake-1' }
citations: 3 chunks, first = Article 33(1) GDPR — In the case of a personal data breach, ...
message_end { finish_reason: 'stop' }
answer: Under the referenced agreement, the processor must implement appropriate technic...
```

Two traps, both measured rather than assumed.

**`es.close()` in `message_end` is not tidiness.** `EventSource` reconnects whenever
the connection ends, and a completed stream ends. Leaving it open and watching the
access log:

```
"GET …/stream" 200 OK
"GET …/stream" 409 Conflict
```

```
message_start #1 at t+16ms
message_end   #1 at t+1280ms
error/close at t+1289ms (readyState=0)   <- CONNECTING: it is retrying
error/close at t+4305ms (readyState=2)   <- CLOSED: the 409 stopped it
```

It reconnects ~3 s later, the replay guard answers `409`, and because a non-`200`
permanently fails an `EventSource`, it then stops. So the cost of forgetting
`close()` is one wasted request, not a loop — and that is only true because
[`message_already_answered`](errors.md#message-already-answered) exists. Do not
remove the 409 handling on the assumption that a well-behaved client never
reconnects. It always reconnects.

**`onerror` is not the server's `error` event.** `EventSource`'s `error` is a plain
`Event` with no `data`; the server's `event: error` arrives as a named listener.
`JSON.parse(e.data)` inside `onerror` throws precisely when the network drops, which
is the moment you least want a second failure. Register them separately.

### 5.2 `fetch` + `ReadableStream` — the explicit path

Use this when you want the errors as errors, no automatic reconnect, and an
`AbortController` you own. It is also the only path available outside a browser.

```js
const controller = new AbortController();
const res = await fetch(url, { headers: { Accept: "text/event-stream" },
                               signal: controller.signal });

// A non-200 is problem+json, not a stream.
if (!res.ok) { console.error("stream refused:", await res.json()); return; }

const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
let buffer = "", answer = "";
outer: for (;;) {
  const { value, done } = await reader.read();
  if (done) break;
  buffer += value.replace(/\r\n/g, "\n");        // sse-starlette frames with CRLF
  let i;
  while ((i = buffer.indexOf("\n\n")) !== -1) {  // one frame
    const frame = buffer.slice(0, i); buffer = buffer.slice(i + 2);
    if (frame.startsWith(":")) continue;         // ": ping" keepalive
    const event = /^event: (.*)$/m.exec(frame)?.[1];
    const data  = frame.split("\n").filter(l => l.startsWith("data: "))
                       .map(l => l.slice(6)).join("\n");
    if (event === "citations") renderSources(JSON.parse(data).chunks);
    else if (event === "token") { answer += JSON.parse(data).text; render(answer); }
    else if (event === "error") showBanner(JSON.parse(data));
    else if (event === "message_end") { controller.abort(); break outer; }
  }
}
```

```
status 200 text/event-stream; charset=utf-8
message_start {"message_id": "2349df9f-28e6-4cf3-b47b-b4f0fa6344c7", "model": "fake-1"}
citations: 3 chunks
message_end { finish_reason: 'stop' }
answer: Under the referenced agreement, the processor must implement appropriate technic...
```

Note that the ticket still travels in the query string here. The endpoint reads
`ticket` as a query parameter only; there is no header form.

### 5.3 Cancellation

Hanging up mid-answer stops generation. Nothing is stored, the question keeps its
`pending` status, and the same message can be streamed again — a dropped stream is
not a consumed one.

```console
$ curl -sN --max-time 1 ".../stream?ticket=$TICKET" > partial.txt
curl: (28) Operation timed out after 1000 milliseconds with 4081 bytes received
```
```json
{"message_id": "6c58f611-00e1-46d0-b5c6-40f3e5c65893", "event": "stream_cancelled_by_client",
 "request_id": "6b9493f4-363c-43bd-b6b9-839c3cd1a3b4", "level": "info",
 "timestamp": "2026-08-01T01:50:08.464457Z"}
```

Exactly one such line per abandoned stream, and none for a completed one.

The message survives intact. Dropping a raw socket 2311 bytes into the answer and then
re-opening the same URL with the same ticket:

```
abandoned after 2311 bytes
re-stream: 200, 41 tokens
```
```
 role      | status   | finish_reason
 user      | answered | -
 assistant | complete | stop
```

One assistant row, not two. A dropped stream stores nothing, so it does not consume
the message.

---

## 6. Errors

Every error leaves this service as `application/problem+json` (RFC 9457) — including
routing `404`s, `405`s, validation failures and unhandled exceptions. There is no
path that returns FastAPI's default `{"detail": …}`.

```json
{
  "type": "http://localhost:8100/docs/errors#conversation-not-found",
  "title": "Conversation not found",
  "status": 404,
  "code": "conversation_not_found",
  "detail": "No such conversation.",
  "instance": "/api/v1/conversations/00000000-0000-4000-8000-000000000000/messages",
  "request_id": "0ea9ed97-0211-4e7e-ac99-ce50e457ad7e"
}
```

**Switch on `code`.** It is a stable machine-readable string. `title` and `detail`
are for humans and may be reworded. `status` may be shared by several codes.

`request_id` is also returned as the `X-Request-Id` header on every response,
including the `500` path, and it is the join key into the structured logs. It is the
one thing worth surfacing in a UI error state.

`type` is an identifier, not a live URL: **it does not resolve over HTTP today**
(`GET /docs/errors` → `404`). It names the anchor in [`docs/errors.md`](errors.md)
in this repository. Full catalogue there.

Validation failures carry one extra field, `errors`, holding Pydantic's own report:

```json
{
  "code": "validation_error",
  "status": 422,
  "errors": [
    {"type": "uuid_parsing", "loc": ["path", "conversation_id"],
     "msg": "Input should be a valid UUID, invalid character: found `n` at 1",
     "input": "not-a-uuid", "ctx": {"error": "invalid character: found `n` at 1"}}
  ]
}
```

`loc` tells you whether the problem is in `body`, `path` or `query`. This is the only
error shape with a field beyond the RFC 9457 members plus `code` and `request_id`.

### 6.1 Refusals: `429`, `503` and `Retry-After`

Two responses are refusals rather than failures. Neither says anything is wrong with
the request; both are admission control (`backend/app/limits.py`) declining to start
work on a shared box with 2.2 GB of RAM.

| Status | `code` | Raised by |
|---|---|---|
| `429` | `rate_limited` | `POST /api/v1/auth/demo-login` — too many tenants from one address |
| `503` | `capacity_unavailable` | `GET …/stream` at its concurrency ceiling, or either endpoint when the limiter cannot reach Redis |

Both carry a **`Retry-After` response header**, in seconds. It is a header rather than
a body member because RFC 9457 defines no field for it and `Retry-After` is already
defined by RFC 9110.

**Read the header, not the sentence.** The `detail` of both says *"Retry after
Retry-After seconds."* literally — the number is in the header and deliberately not in
the prose, because the prose is not a contract. A client that renders `detail` straight
into the UI shows a visitor the words "Retry-After" and no number.

Defaults, all configurable: 30 tenants per address per hour, 16 concurrent streams for
the process and 4 per tenant. `Retry-After` is the remainder of the current window for
`429`, `5` for a stream ceiling and `30` when Redis is unreachable — the last of those
is an operator-scale problem, not a queueing one. The numbers, the reasoning and the
failure behaviour are in
[errors.md](errors.md#admission-control--the-numbers-behind-429-and-503).

**Which address you are metered on** is decided by two *request* headers. A browser
client sets neither, and behind the deployed nginx both are set by the proxy — a value
you send yourself is overwritten before the application sees it.

| Request header | Read when | Effect |
|---|---|---|
| `X-Forwarded-For` | the immediate peer is in `TRUSTED_PROXIES` **and** presents the header below | its **last** entry becomes the metering key |
| `X-LexLoom-Proxy` | `TRUSTED_PROXY_SECRET` is configured | must match it; otherwise `X-Forwarded-For` is ignored and the peer address is used |

Against a local stack, where no secret is configured, position is the whole test — so
`X-Forwarded-For` from a loopback or bridge peer *is* believed, which is how the limit
is exercised in development. That is also why the secret is mandatory on a public box:
without it, anything that can reach the port picks its own bucket.

---

## 7. What a cross-tenant request looks like

The rule: **`404` when revealing existence would cross a trust boundary.** Not `403`.
A `403` on another tenant's resource confirms the resource is there.

Org A creates a conversation. Org B, with a valid token of its own, tries it:

```console
$ curl -s -X POST .../conversations/57e687b6-9e51-474d-b7be-d2941f40d02f/messages \
    -H "Authorization: Bearer $TOKEN_B" -d '{"content":"let me in"}'
HTTP 404
{"type":"…#conversation-not-found","title":"Conversation not found","status":404,
 "code":"conversation_not_found","detail":"No such conversation.",
 "instance":"/api/v1/conversations/57e687b6-…/messages","request_id":"09be14f0-…"}
```

Then two more requests, both from org B: one for org A's *real* message id, one for a
message id that exists nowhere at all. Strip the two members that echo the request,
and compare the bodies:

```console
$ python3 -c "
import json
a = json.load(open('b_streams_As_real_message.json'))     # HTTP 404
b = json.load(open('b_streams_a_message_that_never_existed.json'))  # HTTP 404
for k in ('request_id', 'instance'): a.pop(k); b.pop(k)
print('identical:', a == b); print(json.dumps(a, indent=2))
"
identical: True
{
  "type": "http://localhost:8100/docs/errors#message-not-found",
  "title": "Message not found",
  "status": 404,
  "code": "message_not_found",
  "detail": "No such message in this conversation."
}
```

Byte-identical apart from `request_id` and `instance`. `instance` echoes the path,
so the only identifier in the body is one the caller supplied itself. **A caller
cannot distinguish "someone else's resource" from "no such resource."**

That property is not maintained by the handlers. It falls out of row-level security:
the lookup runs inside the caller's tenant context and another organisation's row is
simply not in the result set, so the handler never has the information it would need
to leak. See [ADR 0003](adr/0003-tenant-isolation.md), including what that does
*not* cover.

---

## 8. Rules a client should encode

- **Switch on `code`, never on `detail`.**
- **`401` is terminal for the current credential.** Do not retry it. Re-run
  `demo-login`. In particular, an API restart rotates the signing key and invalidates
  every outstanding token.
- **`404` means "not yours or not there", and you cannot tell which.** Do not build a
  UI that distinguishes them.
- **`429` and `503` are retryable; read `Retry-After` and honour it.** Both `detail`
  strings name the header instead of a number, so rendering `detail` alone tells the
  visitor nothing. A `503` on a stream means the box is at its concurrency ceiling —
  do not open more streams, close one.
- **`409` on a stream means the answer already exists.** Fetch it rather than
  regenerating — except that today there is no endpoint to fetch it with, so treat
  it as "stop retrying".
- **A ticket is good for one message for 60 seconds.** Mint it immediately before
  opening the stream, not when the message is created.
- **Always `close()` an `EventSource`.**
- **Assume `citations` may be `[]`.** A question whose terms are not in the tenant's
  corpus retrieves nothing, and that is a normal `200` with an empty array, not an
  error.
- **Concatenate `token.text` verbatim.** Trailing spaces are part of the payload.

---

## 9. What this API does not have

Stated because a contract with unstated holes is not a contract.

- **No read endpoints.** No listing, no fetching a conversation, no history. Reload
  the page and the conversation is gone from the UI, though the rows survive in the
  database until the tenant expires.
- **No pagination**, because there is nothing to page.
- **No `Idempotency-Key`**, on any endpoint. A retried `POST /conversations` creates
  a second conversation, and a retried `POST /messages` creates a second question.
- **No CORS.** There is no CORS middleware; a preflight `OPTIONS` returns
  `405 method_not_allowed` and no `Access-Control-*` header is ever sent. The
  frontend must be same-origin behind the reverse proxy.
- **No per-tenant quota, and no global cap on tenant creation.** `POST
  /api/v1/auth/demo-login` is unauthenticated and each call writes an organisation plus
  a 30-row corpus copy — 8.9 KB of chunk text, at 22 ms per call (20 sequential calls
  in 449 ms). It *is* metered, per client address ([§6.1](#61-refusals-429-503-and-retry-after)).
  What that does not bound is a caller spread across many addresses: nothing limits the
  box as a whole, and the 24-hour tenant TTL is what reclaims the rows.
- **No delete or update** of anything. The only removal is the 24-hour tenant purge —
  and that purge is currently declared on a schedule that nothing runs, so today it
  happens only when invoked by hand. See
  [ADR 0001](adr/0001-celery-over-arq.md#the-scheduler-is-declared-and-does-not-run).
- **No `Last-Event-ID` / stream resumption.** Frames carry no `id:`.
- **No `Retry-After` outside `429` and `503`.** Those two carry it; nothing else does,
  and there is no back-pressure signal on a successful response.
- **Two concurrent streams on one message both succeed.** Measured: both return
  `200`, both emit 41 `token` events, and the conversation ends with two assistant
  rows for one question. The `409` guard is a replay guard, not a lock — it fires
  only once an answer has been stored. Harmless with the bundled provider; double
  billing with a metered one.
- **The model is a fake.** `model: "fake-1"` is a canned, deterministic generator
  behind the same async-iterator interface a real provider implements. The streaming,
  cancellation and citation machinery is real; the words are not an answer to your
  question.
