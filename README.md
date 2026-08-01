# LexLoom

A multi-tenant FastAPI backend for a legal-research assistant: ask a question, get
sources first and then a streaming answer, with tenant isolation enforced by the
database rather than by remembering to write `WHERE org_id = …`.

It is a working service, not a slide. Everything below was run against the stack in
`docker-compose.yml`, and the documents in `docs/` paste real output rather than
describing it.

**Start here:**
[**API contract**](docs/api-contract.md) ·
[**Error catalogue**](docs/errors.md) ·
[**ADR 0003 — tenant isolation and what it does not cover**](docs/adr/0003-tenant-isolation.md)

---

## What it does

```console
$ curl -sN ".../conversations/$CONV/messages/$MSG/stream?ticket=$TICKET" -D -
HTTP/1.1 200 OK
cache-control: no-cache
x-accel-buffering: no
content-type: text/event-stream; charset=utf-8
transfer-encoding: chunked

event: message_start
data: {"message_id": "d2f0ab9e-86f0-4e11-bd11-2406578dfa2d", "model": "fake-1"}

event: citations
data: {"chunks": [{"chunk_id": "cf0d6e41-49c7-42df-9389-2242b6602de2",
"org_id": "268bb2a1-43af-4a05-a9ae-9638f5b7fe6f", "content": "Article 33(1) GDPR — In the case
of a personal data breach, the controller shall without undue delay and, where feasible, not
later than 72 hours after having become aware of it, notify the personal data breach to the
supervisory authority […]", "page_from": 52, "page_to": 52,
"score": 0.027581878006458282}, {…}, {…}]}

event: token
data: {"text": "Under "}

event: token
data: {"text": "the "}

… 39 more token frames …

event: message_end
data: {"finish_reason": "stop"}
```

```
first_byte=0.018053s  total=1.289160s  bytes=3399  frames=44
```

Wrapped for width — each `data:` is one line on the wire, and frames are separated by
CRLF. `[…]` is truncated prose and `{…}` are the other two chunks; a `Citation` has
exactly the six members shown. Full object and field semantics in
[the contract](docs/api-contract.md#42-events).

Sources at 18 ms, answer typed out over the following 1.3 s. That ordering is the
whole design: the citations frame is on the wire before the first token, so the wait
is never empty.

Ask *"How long do we have to report a breach?"* and the top-ranked chunk is Article
33(1) — the 72-hour notification rule. Ask about processor obligations and it is
Article 28(3). The retrieval is real Postgres full-text search over a GDPR corpus the
tenant owns; the *words* of the answer are a canned generator behind the same async
interface a real model would implement.

## Architecture

```
                    ┌──────────────┐
   browser ──HTTP──▶│  api         │──asyncpg──▶ Postgres 16 + pgvector
            SSE ◀───│  (FastAPI)   │              row-level security
                    └──────┬───────┘              role: lexloom_app
                           │ Redis
                    ┌──────▼───────┐
                    │  worker      │──asyncpg──▶ same database, same policies
                    │  (Celery)    │              role: lexloom_worker
                    └──────────────┘
```

One image, two processes. The API is async and never blocks; the worker is prefork
and does the CPU-bound work. Both connect as unprivileged roles and **refuse to start**
if the role they get can bypass row-level security.

- **Isolation:** [ADR 0003](docs/adr/0003-tenant-isolation.md)
- **Why Celery and not arq:** [ADR 0001](docs/adr/0001-celery-over-arq.md)
- **Why SSE and not WebSocket:** [ADR 0002](docs/adr/0002-sse-over-websocket.md)

## Run it

No API keys, no accounts, no secrets to obtain. The signing keypair is generated in
memory at start-up and the language model is a local fake.

**1. Bring the stack up.**

```bash
cp .env.example .env
docker compose up -d --build
```

**2. Apply the migrations — before using the service, not after.** They run as the
migration role, which is **not** the role the API connects as, so the DSN has to be
supplied explicitly:

```bash
docker compose exec \
  -e DATABASE_URL='postgresql+asyncpg://lexloom_migrator:migrator@postgres:5432/lexloom' \
  api python -m alembic upgrade head

docker compose exec \
  -e DATABASE_URL='postgresql+asyncpg://lexloom_migrator:migrator@postgres:5432/lexloom' \
  api python -m alembic current
# 0004 (head)
```

Running it with the container's own `DATABASE_URL` fails with
`permission denied for table alembic_version`, which is the design working: the
application role owns nothing and cannot change the schema it is confined by. Nothing
in Compose applies migrations for you.

**3. Check readiness.**

```bash
curl -fsS http://127.0.0.1:8100/health/ready
# {"status":"ready","checks":{"database":true,"redis":true}}
```

Readiness answers exactly one question — *are Postgres and Redis reachable from this
process* — and deliberately no more. It does **not** check that the schema exists: a
completely unmigrated database still returns `{"status":"ready"}`, and the first real
request then fails with a `500`. That is why step 2 comes first. Liveness is narrower
still and touches no dependency at all, so a transient database blip cannot get a
healthy process killed.

**4. Use it.** Walk the four calls in
[docs/api-contract.md §3](docs/api-contract.md#3-the-chat-flow-end-to-end), or start
with one line:

```bash
curl -s -X POST http://127.0.0.1:8100/api/v1/auth/demo-login \
  -H 'Content-Type: application/json' -d '{"label":"Acme Legal"}'
```

Postgres is published on **127.0.0.1:5433**, not 5432 — a development host commonly
runs its own PostgreSQL on 5432, and Docker's port publish then loses to it in a way
that is invisible until `psql` reaches the wrong server.

Compose has no source bind mount, so a code or migration change needs
`docker compose build`. Without a rebuild, `alembic upgrade head` runs against the old
image, no-ops, and reports success.

### Tests

```bash
cd backend
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q          # 120 passed
```

The suite needs the Compose Postgres and Redis running. It is not a mock suite: it
opens raw connections as `lexloom_app`, attempts forbidden operations, drives a real
uvicorn over a real socket, drops that socket mid-answer, and provokes a genuine
Postgres deadlock. CI runs the same thing against service containers.

## What this demonstrates

- **Tenant isolation the database enforces.** Row-level security keyed on a
  transaction-local setting, unprivileged application roles, and a start-up guard that
  refuses to boot as a role which could bypass any of it — including one that is
  merely a *member* of such a role. Cross-tenant requests answer `404`, and the body is
  byte-identical to the `404` for a resource that does not exist anywhere.
- **Streaming that behaves under a real client.** Named SSE events, citations before
  tokens, and disconnect handling that is verified by hanging up a raw socket
  mid-answer rather than by cancelling a task in a unit test — those turn out to be
  two different things.
- **An error contract with no holes.** Every response, including routing `404`s,
  `405`s, validation failures and unhandled exceptions, is RFC 9457
  `application/problem+json` with a stable `code` and a `request_id` that joins to the
  logs. Authentication failures never say *why*.
- **Documentation a frontend developer can work from.** Both stream-consumption paths,
  executed; every error code, captured; and the traps written down — CRLF framing,
  `EventSource` reconnecting after a *successful* stream, `score` being comparable only
  within one response.
- **Decisions with their costs attached.** Each ADR ends with what I got wrong on this
  build and what it cost, not with what I would do at a hundred times the scale.

## Honest limits

This is milestone 1 of a larger design. What is *not* here:

- **No live demo URL.** The service is not deployed yet. Everything above runs locally
  with `docker compose up`.
- **No frontend.** The API is the deliverable at this stage.
- **No real model.** `model: "fake-1"` is a deterministic canned generator behind the
  provider interface. Streaming, cancellation, retrieval and citations are real; the
  prose is not an answer to your question.
- **No document upload, no embeddings.** Retrieval is Postgres full-text search over a
  seeded 30-chunk GDPR corpus, copied into each tenant. The `embedding vector(384)`
  column exists and is unused; hybrid search is milestone 2.
- **No read endpoints.** You can create a conversation, ask, and stream the answer. You
  cannot list or re-fetch anything, so a page reload loses the conversation from the UI
  while the rows survive in the database.
- **No rate limiting, and `POST /auth/demo-login` is unauthenticated.** Each call
  writes an organisation and a 30-row corpus copy. Nothing bounds it. This is the one
  item that must close before the demo faces the public internet.
- **No CORS.** A preflight returns `405`. The frontend must be same-origin behind the
  reverse proxy.
- **No `Idempotency-Key`,** anywhere. A retried `POST` creates a second row.
- **The demo-tenant purge is scheduled and not running.** The 24-hour TTL is declared
  in the Celery beat schedule; no `celery beat` process exists in Compose, so nothing
  enforces it today. See [ADR 0001](docs/adr/0001-celery-over-arq.md#the-scheduler-is-declared-and-does-not-run).
- **`FORCE ROW LEVEL SECURITY` is set on every table and is currently inert,** because
  the migration role is the bootstrap superuser. Explained in full, with the measurement,
  in [ADR 0003](docs/adr/0003-tenant-isolation.md#2-force-row-level-security-is-inert-in-this-deployment).
- **Two concurrent streams on one message both succeed** and leave two assistant rows.
  The `409` is a replay guard, not a lock.

## Demo hardening

The demo is meant to be handed to a stranger, which imposes constraints an internal
tool would not have.

- **A tenant per visitor.** `POST /api/v1/auth/demo-login` provisions a throwaway
  organisation. Shared demo tenants would put one visitor's data in front of the next,
  and a project whose headline claim is per-tenant isolation cannot seat every visitor
  in the same tenant. Open two browsers and they get different `org_id`s and cannot see
  each other.
- **The corpus is copied, not shared.** Each demo tenant owns its own 30 chunks. The
  cheaper design — one corpus organisation with a read-through policy exception — would
  punch a hole in the exact property the project exists to demonstrate.
- **No key material in the repository.** The RSA keypair is generated in memory at
  start-up. A restart rotates the `kid` and invalidates every outstanding token; that is
  the trade for having nothing to leak. The dev identity provider refuses to construct
  itself under `ENV=production`.
- **Ephemeral data.** Demo tenants carry a 24-hour TTL and the purge removes the
  organisation *and its children* in one transaction, keyed on the ids that run just
  destroyed. (Subject to the scheduling gap above.)
- **Failures say nothing useful to a prober.** A rejected token returns one fixed
  string whatever the reason — expired, wrong audience, unknown key, wrong issuer. The
  reason goes to the log. Cross-tenant access returns `404`, never `403`.
- **Tracebacks are logged as strings, not structured frames.** Frame-local logging
  copies every local variable, and in an ingestion path those hold tenant document
  text — an observability change that quietly exports customer data.
- **Bounded containers.** Memory limits are set in Compose because the intended host
  has ~2.2 GB free and no swap, where an unbounded process does not degrade, it takes
  the box down.

## Repository

```
backend/app/        api/v1/  auth/  chat/  db/  worker/     application
backend/alembic/    0001…0004                                schema, roles, policies
backend/tests/      120 tests, real Postgres + real uvicorn
backend/seed/       GDPR excerpts, sourced from EUR-Lex with the reuse basis recorded
docs/               api-contract.md, errors.md, adr/
deploy/             Postgres role bootstrap
```

Everything is English: prose, code, comments and commit history. The commit log is
meant to be readable — each message says what changed and why, and several of them say
what was wrong before.
