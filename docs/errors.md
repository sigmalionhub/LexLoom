# Error catalogue

Every error response from this service is `application/problem+json` per RFC 9457.
The `type` member of each response points at a section of this file:

```
type = "<ERROR_TYPE_BASE_URL>/docs/errors#" + code.replace("_", "-")
```

so `conversation_not_found` → `#conversation-not-found`. The anchors below are named
to match exactly.

`ERROR_TYPE_BASE_URL` defaults to `http://localhost:8100` and is passed through
Compose from `.env`, so setting it there and recreating the `api` service changes the
prefix on every error:

```console
$ sed -i 's|^ERROR_TYPE_BASE_URL=.*|ERROR_TYPE_BASE_URL=https://api.example.test|' .env
$ docker compose up -d api && curl -s localhost:8100/api/v1/nope | jq -r .type
https://api.example.test/docs/errors#not-found
```

`type` is an **identifier, not a live URL**. This service does not serve
`/docs/errors` — `GET /docs/errors` returns `404` whatever the prefix is. It names the
section here.

Every response body below was captured from the running service.

---

## The shape

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

| Member | Stability | Notes |
|---|---|---|
| `code` | **stable** | Switch on this. Never on `detail`. |
| `status` | stable | Matches the HTTP status line. Several codes share a status. |
| `type` | stable | Derived mechanically from `code`. |
| `title` | may be reworded | Short human summary. |
| `detail` | may be reworded | Never carries a reason an attacker could use — see below. |
| `instance` | — | The request path. Echoes only what the caller sent. |
| `request_id` | — | Also the `X-Request-Id` response header. The join key into the logs. |
| `errors` | — | **Only on `validation_error`.** Pydantic's own report. |

Two codes also carry a **`Retry-After` response header**: `rate_limited` and
`capacity_unavailable`. It is a header, not a body member — `Retry-After` is defined by
RFC 9110 §10.2.3 and RFC 9457 says nothing about it. Both are the only errors here that
a client should retry unchanged, and the header is how long to wait.

`request_id` is present on every error, including the `500` path, where it is set
from request state rather than from the context variable — the context variable has
already been reset by the time the catch-all handler runs.

---

## Summary

| Status | `code` | Raised by |
|---|---|---|
| 401 | [`missing_credentials`](#missing-credentials) | Any bearer endpoint, no `Authorization: Bearer …` |
| 401 | [`invalid_token`](#invalid-token) | Any bearer endpoint; also the stream endpoint's ticket |
| 404 | [`conversation_not_found`](#conversation-not-found) | `POST /conversations/{id}/messages` |
| 404 | [`message_not_found`](#message-not-found) | `GET …/stream` |
| 404 | [`source_not_found`](#source-not-found) | `POST /ingest`, `source_key` not in the catalogue |
| 404 | [`not_found`](#not-found) | The router: no route matches |
| 405 | [`method_not_allowed`](#method-not-allowed) | The router: path matches, method does not |
| 409 | [`message_already_answered`](#message-already-answered) | `GET …/stream` on a message that has an answer |
| 409 | [`already_ingested`](#already-ingested) | `POST /ingest`, the caller's organisation already holds this source |
| 422 | [`validation_error`](#validation-error) | Any endpoint, any malformed input |
| 422 | [`generator_unavailable`](#generator-unavailable) | `GET …/stream`, the message asked for a Gemini generator with no `GEMINI_API_KEY` configured |
| 429 | [`rate_limited`](#rate-limited) | `POST /auth/demo-login`, too many from one address |
| 500 | [`internal_error`](#internal-error) | The catch-all |
| 503 | [`capacity_unavailable`](#capacity-unavailable) | `GET …/stream` at its ceiling; either endpoint when the limiter's Redis is unreachable; `POST /ingest` at `INGEST_ORG_CAP` |
| 503 | [`evals_unavailable`](#evals-unavailable) | `GET /evals`, the build carries no `results.json` |

`503` from `GET /health/ready` is **not** in this catalogue: readiness answers with
its own body (`{"status": "degraded", "checks": {…}}`), not with problem+json,
because a probe's consumer is a supervisor rather than a client. It shares a status
with `capacity_unavailable` and nothing else.

The `429` and the `503` are both off by default in the test suite and on everywhere
else; see [Admission control](#admission-control) at the end of this file for the
numbers and for what turns them on.

---

<a id="missing-credentials"></a>
## `missing_credentials` — 401

No `Authorization` header, or one that does not start with `Bearer `.

```console
$ curl -s -X POST http://127.0.0.1:8100/api/v1/conversations \
    -H 'Content-Type: application/json' -d '{"title":"x"}'
```
```json
{
  "type": "http://localhost:8100/docs/errors#missing-credentials",
  "title": "Authentication required",
  "status": 401,
  "code": "missing_credentials",
  "detail": "Expected a Bearer token",
  "instance": "/api/v1/conversations",
  "request_id": "a56bc8ad-ee15-4be1-9297-2da58fc679ac"
}
```

**Client action:** obtain a token via `POST /api/v1/auth/demo-login`. Do not retry
the same request.

---

<a id="invalid-token"></a>
## `invalid_token` — 401

The credential was present and did not verify. Both credentials produce this code;
only `detail` differs, and only to say *which* credential, never why.

Bearer token:

```console
$ curl -s -X POST http://127.0.0.1:8100/api/v1/conversations \
    -H 'Authorization: Bearer not.a.jwt' -H 'Content-Type: application/json' -d '{"title":"x"}'
```
```json
{
  "type": "http://localhost:8100/docs/errors#invalid-token",
  "title": "Invalid credentials",
  "status": 401,
  "code": "invalid_token",
  "detail": "The supplied credentials are not valid.",
  "instance": "/api/v1/conversations",
  "request_id": "b2fb025b-6d78-4c5d-afc0-a5a9391b67af"
}
```

Stream ticket:

```console
$ curl -s "http://127.0.0.1:8100/api/v1/conversations/6c9b4e6d-…/messages/0a6a0902-…/stream?ticket=nope"
```
```json
{
  "type": "http://localhost:8100/docs/errors#invalid-token",
  "title": "Invalid credentials",
  "status": 401,
  "code": "invalid_token",
  "detail": "The stream ticket is not valid for this message.",
  "instance": "/api/v1/conversations/6c9b4e6d-…/messages/0a6a0902-…/stream",
  "request_id": "e7b7dbba-988c-4505-a6e4-c105d618fed2"
}
```

### Why `detail` is fixed

Expired, wrong audience, unknown `kid`, wrong issuer, bad signature, missing
`org_id` — all of them return the same string. A caller that can tell these apart
can probe: an "expired" verdict distinguishes a real ticket from a forged one and
reveals the ticket lifetime, and a "wrong audience" verdict on a stream ticket
confirms that the message id in the URL is a message that exists. The reason is
logged (`token_rejected` / `stream_ticket_rejected` with a `reason` field), where the
operator can read it and the caller cannot.

If you are debugging a `401`, the answer is in the server log line carrying your
`request_id`, not in the response.

### Common causes

- **The API restarted.** The signing keypair is generated in memory at start-up, so
  a restart rotates the `kid` and invalidates every outstanding token and ticket.
- The bearer token is older than 3600 s.
- The ticket is older than 60 s.
- A ticket was sent as a bearer token, or a bearer token as `?ticket=`. Their
  audiences differ (`stream:<message_id>` vs `lexloom-api`), so neither is accepted
  in the other's place.

**Client action:** re-authenticate. Never retry with the same credential.

---

<a id="conversation-not-found"></a>
## `conversation_not_found` — 404

`POST /api/v1/conversations/{conversation_id}/messages` where the conversation is not
visible to the caller's organisation.

```console
$ curl -s -X POST http://127.0.0.1:8100/api/v1/conversations/00000000-0000-4000-8000-000000000000/messages \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"content":"hi"}'
```
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

**"Not visible to the caller" covers both "does not exist" and "belongs to another
organisation", and the response cannot distinguish them.** The lookup runs inside the
caller's tenant context, so another organisation's conversation is not in the result
set at all — the handler never holds the information it would need to leak. A `403`
here would confirm the resource exists, which is the leak this design refuses.

---

<a id="message-not-found"></a>
## `message_not_found` — 404

`GET …/stream` where no message matches all of: the message id, **the conversation id
in the path**, and role `user`, inside the ticket holder's organisation.

```json
{
  "type": "http://localhost:8100/docs/errors#message-not-found",
  "title": "Message not found",
  "status": 404,
  "code": "message_not_found",
  "detail": "No such message in this conversation.",
  "instance": "/api/v1/conversations/6c9b4e6d-…/messages/11111111-1111-4111-8111-111111111111/stream",
  "request_id": "319802fb-7dde-4bbb-a277-f198bc8a16fe"
}
```

The conversation id is a *predicate*, not decoration: a valid ticket does not open its
message under an arbitrary conversation id from the URL. The role clause matters too
— it is what keeps a stored assistant row from being addressable at all, rather than
being found and refused with a `409` that would confirm it exists.

Verified identical, byte for byte apart from `request_id` and `instance`, between a
request for another tenant's real message and a request for a message id that exists
nowhere. See [api-contract.md §7](api-contract.md#7-what-a-cross-tenant-request-looks-like).

---

<a id="source-not-found"></a>
## `source_not_found` — 404

`POST /api/v1/ingest` where `source_key` names no entry in `CATALOGUE`. Checked first,
cheaply, before either admission check touches Postgres — a client that misspells a key
does not spend a reservation to find out.

```console
$ curl -s -X POST http://127.0.0.1:8100/api/v1/ingest \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"source_key":"nonexistent-source"}'
```
```json
{
  "type": "http://localhost:8100/docs/errors#source-not-found",
  "title": "Source not found",
  "status": 404,
  "code": "source_not_found",
  "detail": "No such source in the catalogue.",
  "instance": "/api/v1/ingest",
  "request_id": "06476e3d-8483-4d17-b0f1-1acaf50e7b96"
}
```

`GET /api/v1/catalogue` lists every valid `source_key`; this is a `404` rather than a
`422` because the field itself is well-formed, and only its value fails to name
anything that exists — the same distinction `conversation_not_found` draws from
`validation_error`.

**Client action:** fix `source_key` against `GET /api/v1/catalogue`. This is a bug in
the client; do not retry unchanged.

---

<a id="message-already-answered"></a>
## `message_already_answered` — 409

The message has a stored answer. Generating again would leave two assistant rows for
one question.

```json
{
  "type": "http://localhost:8100/docs/errors#message-already-answered",
  "title": "Message already answered",
  "status": 409,
  "code": "message_already_answered",
  "detail": "This message has been answered; fetch the conversation instead.",
  "instance": "/api/v1/conversations/6c9b4e6d-…/messages/0a6a0902-…/stream",
  "request_id": "9a153fae-b405-4da1-9dcb-0389ed4142ce"
}
```

**This is the code an `EventSource` hits on its automatic reconnect**, and handling it
is not optional. A ticket stays valid for its whole minute, `EventSource` reopens the
connection when a completed stream closes, and browsers retry navigations. Without
this guard each of those would regenerate the answer.

It is a **replay guard, not a lock**: it fires once an answer exists. Two streams
opened concurrently, before either has stored anything, both proceed — see
[api-contract.md §9](api-contract.md#9-what-this-api-does-not-have).

`detail` says "fetch the conversation instead", which is currently advice you cannot
take: there is no read endpoint. Treat it as "stop retrying".

**Client action:** stop. Do not retry, do not mint a new ticket.

---

<a id="already-ingested"></a>
## `already_ingested` — 409

`POST /api/v1/ingest` where the caller's own organisation already holds chunks for the
requested `source_key` — the duplicate guard is keyed on the (organisation, source)
pair, not on the organisation alone, so a tenant may still ingest a *different*
catalogue source after this fires. Checked inside `ensure_ingest_admitted`, before the
global reservation is taken.

```console
$ curl -s -X POST http://127.0.0.1:8100/api/v1/ingest \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"source_key":"gdpr-full"}'
```
```json
{
  "type": "http://localhost:8100/docs/errors#already-ingested",
  "title": "Already ingested",
  "status": 409,
  "code": "already_ingested",
  "detail": "This organisation has already ingested a document from the catalogue.",
  "instance": "/api/v1/ingest",
  "request_id": "61e9412e-6a20-4309-9d3c-3116a15cb5d8"
}
```

The example above is the common case: `gdpr-full` is preloaded into every organisation
at sign-in (`PRELOADED_SOURCE_KEYS`), so it already has chunks before a caller ever
asks. Unlike `capacity_unavailable`, this carries no `Retry-After` — retrying changes
nothing until the organisation itself is gone, which is the opposite of what
`Retry-After` promises a client.

**Client action:** stop. `GET /api/v1/ingest/status` already shows this source as
`complete`; do not retry.

---

<a id="not-found"></a>
## `not_found` — 404

No route matches the path. Emitted by the router, mapped into problem+json so that a
client never has to parse two different error shapes.

```console
$ curl -s http://127.0.0.1:8100/api/v1/nope
```
```json
{
  "type": "http://localhost:8100/docs/errors#not-found",
  "title": "Not Found",
  "status": 404,
  "code": "not_found",
  "detail": "Not Found",
  "instance": "/api/v1/nope",
  "request_id": "0929998c-6c02-4842-9388-951864c2f8c8"
}
```

`title` and `detail` are both the router's own phrase here, so they carry no more
information than `code` does. Distinguish this from `conversation_not_found` and
`message_not_found`, which mean the route exists and the resource does not.

---

<a id="method-not-allowed"></a>
## `method_not_allowed` — 405

The path matches a route registered for a different method. The `Allow` header is
preserved.

```console
$ curl -s -X DELETE http://127.0.0.1:8100/api/v1/conversations -H "Authorization: Bearer $TOKEN" -D -
```
```http
HTTP/1.1 405 Method Not Allowed
allow: POST
content-type: application/problem+json
```
```json
{
  "type": "http://localhost:8100/docs/errors#method-not-allowed",
  "title": "Method Not Allowed",
  "status": 405,
  "code": "method_not_allowed",
  "detail": "Method Not Allowed",
  "instance": "/api/v1/conversations",
  "request_id": "cd58d2f4-255c-4032-8ae2-8f1798043e35"
}
```

A cross-origin preflight lands here: there is no CORS middleware, so `OPTIONS`
matches no route and returns this.

---

<a id="validation-error"></a>
## `validation_error` — 422

Any input that fails schema validation — body, path parameter or query parameter.
This is the only error carrying an extra member, `errors`, holding Pydantic's report
verbatim.

Body field too short:

```console
$ curl -s -X POST http://127.0.0.1:8100/api/v1/conversations \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"title":""}'
```
```json
{
  "type": "http://localhost:8100/docs/errors#validation-error",
  "title": "Invalid request",
  "status": 422,
  "code": "validation_error",
  "detail": "The request body or parameters failed validation.",
  "instance": "/api/v1/conversations",
  "request_id": "fcebe6e2-130f-44b2-916d-2ded53af5265",
  "errors": [
    {"type": "string_too_short", "loc": ["body", "title"],
     "msg": "String should have at least 1 character", "input": "",
     "ctx": {"min_length": 1}}
  ]
}
```

Missing query parameter — the stream endpoint without `?ticket=`:

```json
"errors": [
  {"type": "missing", "loc": ["query", "ticket"], "msg": "Field required", "input": null}
]
```

Malformed path parameter:

```json
"errors": [
  {"type": "uuid_parsing", "loc": ["path", "conversation_id"],
   "msg": "Input should be a valid UUID, invalid character: found `n` at 1",
   "input": "not-a-uuid", "ctx": {"error": "invalid character: found `n` at 1"}}
]
```

Malformed JSON body:

```json
"errors": [
  {"type": "json_invalid", "loc": ["body", 1], "msg": "JSON decode error",
   "input": {}, "ctx": {"error": "Expecting property name enclosed in double quotes"}}
]
```

A demo label with nothing visible in it — here a single U+3164 HANGUL FILLER, which
is an ordinary `Lo` letter that draws nothing and is the canonical blank-username
character:

```console
$ curl -s -X POST http://127.0.0.1:8100/api/v1/auth/demo-login \
    -H 'Content-Type: application/json' -d '{"label":"ㅤ"}'
```
```json
"errors": [
  {"type": "value_error", "loc": ["body", "label"],
   "msg": "Value error, label must contain at least one character that renders",
   "input": "ㅤ", "ctx": {"error": {}}}
]
```

### Field constraints

| Endpoint | Field | Rule |
|---|---|---|
| `POST /auth/demo-login` | `label` | 1..64 after whitespace stripping; not entirely non-rendering |
| `POST /conversations` | `title` | 1..255 |
| `POST /conversations/{id}/messages` | `content` | 1..8000 |
| `POST /auth/stream-tickets` | `message_id` | UUID |
| `GET …/stream` | `ticket` | required query parameter |

**Client action:** `loc[0]` tells you whether to fix the body, the path or the query.
This is a bug in the client; do not retry unchanged.

`errors` mirrors Pydantic's structure and follows Pydantic's version. Read `loc` and
`type`; do not depend on the exact wording of `msg`.

---

<a id="generator-unavailable"></a>
## `generator_unavailable` — 422

`GET …/stream` for a message whose `generator` names a Gemini choice
(`gemini-3.5-flash-lite` or `gemini-3.6-flash`) while this server has no
`GEMINI_API_KEY` configured. Raised from `build_provider`, before the stream slot is
reserved and before the response exists — once `EventSourceResponse` starts, the status
line is already committed to `200` and there is no way back to a problem+json body.

```json
{
  "type": "http://localhost:8100/docs/errors#generator-unavailable",
  "title": "Generator unavailable",
  "status": 422,
  "code": "generator_unavailable",
  "detail": "gemini-3.5-flash-lite is not available on this server: no Gemini API key is configured. GET /api/v1/generators lists what is.",
  "instance": "/api/v1/conversations/6c9b4e6d-…/messages/0a6a0902-…/stream",
  "request_id": "d4b3fbb0-2c8e-4a1a-9c4a-6b5b0b6f6c11"
}
```

This is the residual path for a caller that posted a Gemini choice directly, or before
a key was removed — `GET /api/v1/generators` plus the frontend disabling a Gemini
option with no key is what keeps it unreachable through the UI in the normal case:
the endpoint lists exactly which generator choices would actually work right now, and
a client built from it never offers one this server cannot honour. `generator` is
validated against the enum at `POST .../messages` time and
re-read from the stored row here, so this fires on the stream open that follows a POST
made while a key was configured, or made directly against the API.

**Client action:** build the generator picker from `GET /api/v1/generators`, which
lists only what would work right now. This is a bug in the client; do not retry
unchanged.

---

<a id="rate-limited"></a>
## `rate_limited` — 429

Too many throwaway organisations created from one client address inside one window.
`POST /api/v1/auth/demo-login` is the only endpoint that raises it.

```console
$ curl -s -X POST http://127.0.0.1:8100/api/v1/auth/demo-login \
    -H 'Content-Type: application/json' -d '{"label":"Acme Legal"}' -D -
```
```http
HTTP/1.1 429 Too Many Requests
content-type: application/problem+json
retry-after: 1393
x-request-id: 582a892f-d2d4-415e-82e4-31165a60b0d5
```
```json
{
  "type": "http://localhost:8100/docs/errors#rate-limited",
  "title": "Too many requests",
  "status": 429,
  "code": "rate_limited",
  "detail": "Too many demo organisations from this address. Retry after Retry-After seconds.",
  "instance": "/api/v1/auth/demo-login",
  "request_id": "582a892f-d2d4-415e-82e4-31165a60b0d5"
}
```

`detail` carries no numbers: not the limit, not how much of it is spent, not how many
tenants the address already owns. `Retry-After` says the only thing a client needs in
order to behave, and the rest is in the log line
(`demo_login_rate_limited`, with the address and the count).

**Every request counts, including one that would have failed validation.** The limit is
spent in a route dependency, before the body is parsed, so a client looping on a
malformed label exhausts its budget exactly as fast as one sending valid ones. That is
deliberate: the cost this bounds is the request, not the row.

The window is fixed rather than sliding, so an address that spends its budget at the end
of one window and again at the start of the next makes **up to twice the limit** in a
short burst. Bounded and cheap; the alternative costs about ten times the memory in a
Redis capped at 48 MB with `allkeys-lru`, where what gets evicted is the counter itself.

**Client action:** wait `Retry-After` seconds and retry the same request. This is the one
`4xx` in this catalogue that is a retry signal. If you already hold a demo token, use
it — it is valid for 3600 s and a second login buys nothing.

---

<a id="capacity-unavailable"></a>
## `capacity_unavailable` — 503

The service will not *start* this work now. Four triggers, one code. The first three
are `GET …/stream`'s admission control (`app/limits.py`), and share a client action:

1. the caller's organisation already has its maximum number of streams open;
2. the whole process is at its concurrent-stream ceiling;
3. Redis is unreachable, so neither ceiling can be consulted — and the limiter refuses
   rather than proceeding unmetered, which is the outcome it exists to prevent.

```console
$ curl -s "http://127.0.0.1:8100/api/v1/conversations/75c64440-…/messages/5200239f-…/stream?ticket=$TICKET" -D -
```
```http
HTTP/1.1 503 Service Unavailable
content-type: application/problem+json
retry-after: 5
x-request-id: bd26ed00-23ba-45c9-95f2-d80b00a94d10
```
```json
{
  "type": "http://localhost:8100/docs/errors#capacity-unavailable",
  "title": "Capacity unavailable",
  "status": 503,
  "code": "capacity_unavailable",
  "detail": "The service cannot start this work right now. Retry after Retry-After seconds.",
  "instance": "/api/v1/conversations/75c64440-…/messages/5200239f-…/stream",
  "request_id": "bd26ed00-23ba-45c9-95f2-d80b00a94d10"
}
```

`Retry-After` is **5** for a ceiling and **30** for an unreachable Redis, which is the
only outward difference between them. Which one fired is in the log:
`stream_admission_refused` carries `reason` (`org_ceiling` or `global_ceiling`) with both
counts, and `limits_backend_unavailable` carries `check`. Telling the caller apart from
the operator is the same trade [`invalid_token`](#invalid-token) makes: a client
that could distinguish "your tenant is full" from "the box is full" could measure how
much of the box it holds.

This is raised **before** the stream opens — a 503 status line, not a `200` with an error
event inside it — so `EventSource` reports it as a connection failure rather than
rendering an empty answer.

The ticket is not consumed. A ticket is valid for 60 s, so a retry inside that window can
reuse it; past that, mint a new one from `POST /auth/stream-tickets`.

**Client action:** wait `Retry-After` seconds and retry. Do not open more streams in the
meantime — the ceiling is on concurrency, so a client that retries by opening a second
connection is competing with itself.

### The fourth trigger: `POST /api/v1/ingest`

A different backend entirely, and the same code because the client action is the same:
`INGEST_ORG_CAP` (16) organisations already hold a corpus or a live reservation
(`public.reserve_ingest_slot`, migration 0008), counted in Postgres rather than in
Redis. `detail` says so — it is the one outward difference from the stream trigger:

```json
{
  "type": "http://localhost:8100/docs/errors#capacity-unavailable",
  "title": "Capacity unavailable",
  "status": 503,
  "code": "capacity_unavailable",
  "detail": "The service cannot start this ingest right now. Retry after Retry-After seconds.",
  "instance": "/api/v1/ingest",
  "request_id": "7a2b6c1e-9f3d-4e5a-8b2c-1d4f6e8a0c3b"
}
```

`Retry-After` is **300** here, not 5 or 30 — a floor for a polite client, not a
forecast: the cap only actually relaxes on the hourly purge, and telling a client to
wait an hour invites it to give up. There is no ticket to reuse and no reservation held
open by this response: nothing was admitted, so nothing needs releasing.

**Client action:** wait `Retry-After` seconds and retry the same request. Do not retry
by polling faster — the cap does not move between the hourly purges regardless.

---

<a id="evals-unavailable"></a>
## `evals_unavailable` — 503

`GET /api/v1/evals` when this build carries no `app/evals/results.json` — decided
before reading anything else, and never `200` with zeros: a table of zeros reads as
"retrieval is broken" rather than as "this build did not carry the artefact," which is
the true statement and the actionable one.

```json
{
  "type": "http://localhost:8100/docs/errors#evals-unavailable",
  "title": "No retrieval measurement in this build",
  "status": 503,
  "code": "evals_unavailable",
  "detail": "app/evals/results.json is missing from this build, so there are no retrieval numbers to report. Run `python -m app.evals.run --write` and commit the result.",
  "instance": "/api/v1/evals",
  "request_id": "3f8c2a6e-5d1b-4c9a-8e7f-2b4a6c8e0d1f"
}
```

Unlike the other `503` in this catalogue, this one carries no `Retry-After`: a missing
artefact is fixed by a build, not by waiting. `GET /api/v1/evals` reads the file once
per process (`lru_cache`) and never touches the database — this status means the file
was absent when the process started, not that anything just failed.

**Client action:** this is not a transient condition. Report it; do not retry.

---

<a id="internal-error"></a>
## `internal_error` — 500

An unhandled exception reached the catch-all. The body deliberately says nothing
about the cause.

The easiest way to see a real one is the state a reader is in between steps 1 and 2 of
the README: the stack is up and the migrations have not been applied, so the first
write hits a table that does not exist.

```console
$ curl -s -X POST http://127.0.0.1:8100/api/v1/auth/demo-login \
    -H 'Content-Type: application/json' -d '{"label":"Acme Legal"}' -D -
```
```http
HTTP/1.1 500 Internal Server Error
content-type: application/problem+json
x-request-id: 3814bcce-01f8-4e53-9f63-2c927652a933
```
```json
{
  "type": "http://localhost:8100/docs/errors#internal-error",
  "title": "Internal error",
  "status": 500,
  "code": "internal_error",
  "detail": "An unexpected error occurred. Quote the request_id when reporting it.",
  "instance": "/api/v1/auth/demo-login",
  "request_id": "3814bcce-01f8-4e53-9f63-2c927652a933"
}
```

Note what the body does *not* say: not the table, not the statement, not the SQLSTATE.
Only the `request_id`, which is also the `X-Request-Id` header, and which matches the
log line exactly.

The full traceback is logged as a **string**, not as structured frames:

```
log keys: ['event', 'exception', 'level', 'path', 'request_id', 'timestamp']
exception is a str — first line: Traceback (most recent call last):
                     last line:  (Background on this error at: https://sqlalche.me/e/20/f405)
request_id: 3814bcce-01f8-4e53-9f63-2c927652a933
```

Six keys, and no frame locals among them. Structured frame logging carries every
frame's local variables, and in an ingestion path those locals hold tenant document
text — an observability change that quietly copies customer data into the log stream.
The `request_id` is how a caller points at the log line without the service having to
hand them the cause.

**Client action:** show the `request_id` to the user, and stop. A `500` here is not a
retry signal; nothing about the request will have changed.

---

<a id="admission-control"></a>
## Admission control — the numbers behind `429` and `503`

Both ceilings live in `backend/app/limits.py` and are counted in Redis, so they mean the
same thing to every process that serves a request. The defaults are sized for the
deployment in [deploy/README.md](../deploy/README.md): 2 vCPU, 2.2 GB free and 2 GB of
swap, **shared with other applications**. Memory pressure there is felt by all of them,
and the OOM kill at the end of it picks its victim by the kernel's scoring rather than
by who caused it — so it may take a neighbouring application rather than this one.

| Setting | Default | Why that number |
|---|---|---|
| `DEMO_LOGIN_LIMIT` | 30 | Per address per window. One demo-login writes **1009 rows and 1.95 MiB** — an organisation plus the 1008 chunks every tenant is born holding — so 30/hour bounds one address at ~30k rows and ~59 MiB an hour, and the hourly purge does not cap the accumulation because it collects only tenants whose 24-hour TTL has lapsed. The value is a behavioural one and is unchanged: a visitor needs one tenant, two demonstrate the isolation thesis in two browsers, and 30 still covers a NATed office where thirty people look in the same hour. What that admission costs is worked out in `backend/app/config.py` beside the default. |
| `DEMO_LOGIN_WINDOW_SECONDS` | 3600 | |
| `MAX_CONCURRENT_STREAMS` | 16 | The SQLAlchemy pool is 10 + 10 overflow. A stream holds no connection while generating but takes one at each end, so 16 leaves four connections for everything that is not a stream even if every admitted stream reaches at the same instant. |
| `MAX_CONCURRENT_STREAMS_PER_ORG` | 4 | A tab, its `EventSource` reconnect and a second tab all fit, and at least four different organisations can always be streaming. |
| `STREAM_SLOT_TTL_SECONDS` | 600 | How long a slot may go **without a sign of life** before the sweep collects it. Not a cap on stream length: a running stream refreshes its own slot every 150 s, so the sweep only ever collects one whose process died holding it. |
| `TRUSTED_PROXIES` | `127.0.0.1,::1` in code, `127.0.0.1,::1,172.16.0.0/12` as `docker-compose.yml` ships it | Whose `X-Forwarded-For` is believed, or `none`. See below. |
| `TRUSTED_PROXY_SECRET` | unset in code, **required when `ENV` is `demo` or `production`, unless `TRUSTED_PROXIES=none`** | Proves a request came *through* nginx, not merely from its direction. See below. |
| `LIMITS_ENABLED` | `true` | |

The code default for `TRUSTED_PROXIES` is loopback because that is the safe answer for a
process reached directly. It is the wrong answer inside a container — the port is
published on the *host's* loopback, so nginx's connection is forwarded in and the peer is
the Compose bridge gateway — which is why Compose widens it and why the row above gives
both values.

**Nothing here caps how long a stream may run.** An earlier version of this section
claimed nginx's `proxy_read_timeout 300s` did. It does not: that is an *inactivity*
timeout between successive reads, and `EventSourceResponse(ping=15)` writes every fifteen
seconds, so nginx holds a live stream indefinitely. The slot is kept alive by the stream
refreshing it, not by the stream being short.

### Which address a request is metered against

`X-Forwarded-For` is read **only** when the immediate peer is in `TRUSTED_PROXIES`, and
then the **last** entry is taken, never the first. `deploy/nginx/snippets/lexloom-proxy.conf`
sets the header to `$remote_addr` rather than `$proxy_add_x_forwarded_for` — overwriting
rather than appending — so it arrives carrying exactly one value: the address nginx
accepted the connection from. Reading the last entry is correct under that vhost and
stays correct under the conventional appending form, where a caller controls every entry
but the last.

Position is necessary and not sufficient. Every connection through the published port
reaches the container from the bridge gateway, so on a shared host "the peer is a trusted
proxy" is true of anything that can open a socket to it — including a server-side request
forgery in a neighbouring application. When `TRUSTED_PROXY_SECRET` is set, the peer must
also present it in `X-LexLoom-Proxy`, which nginx sends and nothing else on the box does.

The exact rule, because it is a start-up refusal and a half-remembered version of it
costs a deployment: `Settings` refuses to construct when `ENV` is **`demo` or
`production`** *and* `TRUSTED_PROXIES` names at least one network *and* the secret is
empty. All three. With `TRUSTED_PROXIES=none` the forwarding header is never read and
there is nothing for a secret to protect, so no secret is demanded — which is how a
process that serves no HTTP at all, such as the Celery worker, runs on a public box
without holding one.

A caller who reaches the port directly is metered on its socket address, whatever it
claims to forward. IPv6 is bucketed by **/64**, because a customer delegated a /64 would
otherwise have 2^64 budgets — and an IPv4-mapped address (`::ffff:203.0.113.9`, which is
what a dual-stack listener reports) is unwrapped first, or the whole mapped range would
collapse into one bucket. The honest residual: a customer delegated a **/56** holds 256
of these buckets and a **/48** holds 65,536, so an IPv6 visitor's budget scales with the
size of their delegation.

The container starts uvicorn with `--no-proxy-headers`. Without it, uvicorn's own
`ProxyHeadersMiddleware` rewrites `scope["client"]` from the header first, by a second
trust list configured nowhere in this repository — so the application would meter an
address uvicorn chose.

### Turning them off

`LIMITS_ENABLED=false` is how the test suite runs: it creates well over a hundred
organisations from `127.0.0.1`, which is one address and therefore one budget. `Settings`
**refuses to construct** with `limits_enabled=False` when `ENV` is `demo` **or**
`production`, so the process does not start rather than starting unprotected.

The `demo` half of that is the whole point. Keyed on `production` alone — as it first was
— the check was dead code on the only box it was written for: that deployment runs the
development identity provider, which refuses to start under `ENV=production`, so `ENV`
there can never be the value the guard tested. `demo` is the name for what that box
actually is, and the same taxonomy fixes `JwksCache(require_https=…)`, which was
decorative there for exactly the same reason.

Whichever way it is set, the process **says so once at start-up** — `limits_enabled` at
info with the numbers, or `limits_disabled` at warning. An absent limit that announces
itself is an operational problem; a silent one is an incident.
