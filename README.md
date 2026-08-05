# LexLoom

A multi-tenant document-research API. Ask a question against a document corpus, get the
sources first and then a streaming answer, with tenant isolation enforced by the database
rather than by remembering to write `WHERE org_id = …`.

FastAPI over Postgres 16 + pgvector, Celery for the CPU-bound work, and a Next.js static
export that instruments the backend instead of decorating it. It is a working service,
not a slide: the documents in `docs/` paste real output rather than describing it, and
**every number below names the file it lives in or can be recomputed from one** — with one
exception, which says so where it stands.

This repository is private and stays private. There is no public demo URL and no CI badge
to point at. What there is instead is a stack that comes up with `docker compose`, a test
suite that talks to a real Postgres and a real socket, and a retrieval measurement that
fails the build when it regresses.

**Start here:**
[**API contract**](docs/api-contract.md) ·
[**Error catalogue**](docs/errors.md) ·
[**ADR 0003 — tenant isolation and what it does not cover**](docs/adr/0003-tenant-isolation.md) ·
[**Deployment runbook**](deploy/README.md)

---

## Read this part first: the measurement caught the corpus being garbage

The second milestone added retrieval evaluation — thirty hand-written questions, graded
relevance judgements, three search modes, a CI gate. The first thing it produced was not
a score. It was a contradiction it could not explain, and running that down turned out to
be worth more than every feature in the milestone.

**The PDF parser was silently destroying the text.** `pypdf` splits this document's words
at nearly every glyph-run boundary. The extracted prose reads

```
"Ha ving regar d to the opinion"          for   "Having regard to the opinion"
"Af ter transmission of the draf t"       for   "After transmission of the draft"
```

Over the first twenty pages it produced **zero intact occurrences of the word "Article"**
and made **9.3 % of extracted tokens a single stray letter**. That is not a cosmetic
defect. Postgres full-text search indexed `regar` and `d` as two lexemes; the embeddings
were computed over noise wearing the shape of sentences. **Every number measured before
this was found was measured against nonsense.**

Four extractors, same twenty pages, so the numbers are comparable
(`backend/tests/test_ingest_parse.py`):

| extractor | intact "Article" | single-letter tokens |
|---|---|---|
| `pypdf`, default | **0** | 9.3 % (1815 / 19569) |
| `pypdf`, `extraction_mode="layout"` | **0** | 2.8 % (462 / 16686) |
| `pdftotext` (poppler, system binary) | 7 | 2.1 % (340 / 15905) |
| **`pdfminer.six` — chosen** | **7** | **2.1 % (340 / 15908)** |

`pdfminer.six` ties poppler exactly on both, and it is a pip package: no line in
`backend/Dockerfile`, no apt step in CI, nothing to install on a shared deploy box. The
tie is what decided it — the argument is written out in `backend/app/ingest/parse.py`.

**The corpus went from 1027 chunks to 978, and every eval number was re-measured.**

Three things about this are the actual case study, and they are why it leads:

- **The harness found it, not a person reading the text.** Nobody proofreads a
  thousand-chunk corpus. What surfaced it was a retrieval metric that did not behave the
  way the questions said it should, chased instead of accepted.
- **The task that found it refused to fix it.** Replacing the extractor renumbers every
  chunk ordinal and invalidates the gold set and the manifest together, so fixing it
  inside the eval task would have silently rebased every human judgement onto chunks
  nobody had judged. It escalated instead.
- **A guard that proves stability is not a guard that proves correctness.** The chunker
  already had a test pinning chunk digests against a committed fixture. It stayed green
  through the entire life of the broken parser, because garbled text is perfectly stable.
  The readability tests that now exist —
  the two known-damaged phrases surviving intact, and a ceiling on the single-letter
  token rate — were required to be **proved red against `pypdf`** before they counted.

That last test also states what it does *not* catch, which matters more than the test:
the 5 % ceiling catches `pypdf`'s default mode at 9.3 %, and **does not** catch its layout
mode at 2.8 %. Closing that gap needs a ceiling below 2.8 %, which leaves 0.32 points of
margin over the 2.48 % the current corpus actually measures — a guard that goes red on
ordinary corpus drift and then gets raised by whoever is unblocking CI that day. The
reasoning is in the test file, next to the threshold.

## Almost every metric fell after the fix, and that is the measurement getting honest

Under the broken extractor most query words matched almost nothing, so whatever did match
was trivially top-ranked. Clean text puts far more chunks in play for the same query, and
ranking them is a harder problem than ranking a handful. The scores went down; retrieval
went up.

**The pre-fix numbers are not in this repository and are not a baseline.** `results.json`
holds one measurement, taken after the fix, carrying the commit and the date it was taken
at. Comparing today's numbers against the ones the broken corpus produced would be
comparing against a measurement of nothing, and there is deliberately nothing here to
make that mistake with.

## What the retrieval actually measures

All of it from [`backend/app/evals/results.json`](backend/app/evals/results.json), gated by
[`floors.json`](backend/app/evals/floors.json). The `evals` job in
[`.github/workflows/ci.yml`](.github/workflows/ci.yml) recomputes every cell from scratch
on each push and fails the build against those floors.

That job has **not run yet**: this milestone has not been pushed. What has been done is the
run it performs, executed locally — a full reseed and re-embed reproducing all fifteen cells
exactly, three times, on this machine. Saying "CI verifies it" of a job that has never
executed would be the same defect this page spends a section on, one page earlier.

Corpus: **1008 chunks** per evaluation tenant — 978 from the full GDPR PDF plus the
30 seeded excerpts. Thirty questions, of which **six are unanswerable by design.**

| mode | P@3 | recall@5 | MRR@10 | nDCG@10 |
|---|---|---|---|---|
| `fts` | **0.178** | 0.148 | **0.420** | **0.240** |
| `vector` | 0.111 | 0.152 | 0.308 | 0.210 |
| `hybrid` | 0.144 | **0.176** | 0.315 | 0.226 |

**The ceilings belong next to the numbers, not in a footnote:**

- **P@3 cannot exceed 0.80.** Six of the thirty questions have no answer in this corpus
  at all, so their precision is the constant 0 in every mode at every quality. 24/30.
- **recall@5 cannot exceed 0.9137.** Nine of the 24 answerable questions have more than
  five relevant chunks (six with 6, two with 7, one with 10), so five rows physically
  cannot cover them. The full arithmetic is in `floors.json`, under
  `hybrid.recall_at_5.why`.
- **The gold set leaves heading chunks unjudged in nine of the twenty-four articles its
  own reasons name.** Some articles have their heading-plus-run-up chunk graded as
  supporting evidence and some do not; the convention is not applied uniformly. That bias
  runs *against* the retriever — an unjudged chunk that gets retrieved counts as a miss —
  so **the measured MRR is a floor on true quality, not an inflation of it.** It is
  disclosed for exactly that reason, and fixing it properly means a systematic pass over
  all thirty questions run **blind**, before anyone sees what it does to the scores.
  (That count of nine came out of the gold-set review, not out of a file in this tree. It
  is the one figure on this page you cannot check from the repository, and it is here
  because it cuts against the numbers rather than for them.)

**What these numbers do not say, stated because the obvious readings are all wrong:**

- **There is no synonym win for the vector mode.** The design predicted one and the
  measurement refused it: on the six synonym questions, `vector` scores **MRR@10 0.209**
  against `fts`'s **0.352**. It loses. The only synonym lead anyone has is `hybrid`'s
  **0.375**.
- **`vector` does not beat `fts` overall.** It loses P@3, MRR@10 and nDCG@10. It leads
  recall@5 by 0.0042 — and one rank flip on this gold set is worth 0.0208
  (`floors.json`, `headroom_rule.one_flip`), so that lead is a fifth of the smallest
  movement the metric can make. **It is not a claimable win either.**
- **`hybrid` is not "best".** Of the four headline metrics it leads recall@5 and nothing
  else (0.176), and reciprocal-rank fusion does not beat its best leg at ranking. Its one
  other genuine lead is the synonym MRR above.
- **The one place `vector` earns its keep** is paraphrase recall — 0.233 against `fts`'s
  0.119 — while ranking those same chunks worse (MRR 0.250 against 0.429). The
  defensible sentence is: *on paraphrases the vector mode finds more relevant chunks than
  lexical search does, and ranks them worse.*
- **Only `fts`'s answerability AUC means anything** (0.7222). `hybrid`'s is computed over
  a near-constant — with two legs at k=60 its top score is structurally confined to the
  interval [1/61, 2/61], one rank step wide — and `vector`'s unanswerable questions score
  0.490–0.811, straight through the 0.385–0.855 its answerable ones score. Both are
  withheld from the demo page, with the omission and its two different reasons printed
  where the number would have been.

**The CI gate.** Ten floors, one per meaningful cell, each derived from a stated rule
rather than chosen: `floor = round_down(measured − one_flip × mode_factor)`. The rule is
machine-readable in `floors.json` and a test evaluates it against every entry, so the
file cannot describe a rule its own numbers do not follow. **Every floor is marked
`provisional: true`** — they were set without the human review of the thirty questions
they derive from, `--check` says so on stderr on every run, and the marker records who
has vouched for the number rather than how much it is trusted. A provisional floor gates
a build exactly as a reviewed one does.

## How the gold set was written

This is the part that makes the numbers evidence rather than decoration, and it is quoted
from [`backend/app/evals/goldset.py`](backend/app/evals/goldset.py) because that is where
it is enforced:

> A gold set is trivially easy to write so that it flatters whatever the system already
> does: pick questions whose wording shares a rare term with exactly one chunk and lexical
> search looks excellent; write the questions after watching the vector mode succeed and
> the vector mode looks excellent. Either way the metric measures the author.
>
> The procedure used here, in order, and none of it reversed:
>
> 1. The question text was written first, from the regulation's own structure — which
>    articles exist, what each is about — and from the vocabulary a person researching
>    GDPR uses, not the vocabulary the corpus uses. Written and then frozen.
> 2. Only then were the relevant chunks located, by walking to the article by its number
>    in the parsed text (`Article 33`, `Article 15`), never by searching for the
>    question's own words. Locating ground truth by keyword overlap is how a gold set ends
>    up defining relevance as "whatever the lexical mode would find".
> 3. `search_chunks` was not run against any question until every judgement in this file
>    was fixed and the manifest written. The numbers in `results.json` are the first time
>    these questions met the retriever.

Every one of the 124 judgements carries a `why` in prose, because a judgement with no
reason cannot be audited. Those reasons are what made the corpus fix survivable: when the
re-parse renumbered every chunk, the judgements were re-bound by matching the recorded
reason to the new text rather than re-judged from scratch.

One case is worth reading in full. During the re-bind, one ambiguous judgement had a
plausible alternative binding — and measurement showed the alternative sat at rank 1–3 in
all three modes, so rebinding it would have converted a miss into a hit everywhere at
once. It was not rebound, and the reason it was not is the test: *would I be doing this if
the numbers had not been shown to me?*

## Six right numbers with wrong reasons

This milestone produced six correct measurements travelling with an explanation nobody had
checked. Each was caught only because a reviewer was told to verify the *reasoning* rather
than the result. They are listed because the pattern is more useful than any of the six:

| the number | the explanation attached to it | what was true |
|---|---|---|
| 1027 → 978 chunks | "the `Article` boundary rule stopped being dead code" | Ablating that rule moves the count 978 → 980. It accounts for **2** chunks. `backend/app/ingest/catalogue.py` |
| 978 chunks | "ablating the 15 false-positive boundaries moves it to 979" | It moves it to **978**. Delta zero. `backend/app/ingest/parse.py` |
| keep the higher grade on a merge | "`recall_at_k` and `ndcg_at_k` both read the grade" | `recall_at_k` is binary in `grade > 0` and cannot see it. `backend/app/evals/goldset.py` |
| ten CI floors | a prose rule the file's own numbers did not follow | The rule is now data with a test over all ten entries. `backend/app/evals/floors.json` |
| hybrid's AUC is meaningless | "its top score takes only the values 1/61 and 2/61" | It **spans** that interval and takes 19 distinct values. `frontend/components/panel/evals-table.tsx` |
| a latency ladder's sizing | "the 45 s `gemini-3.6-flash` was measured at" | **Nothing ever measured 45 s.** It came from an illustrative selector mock-up in a plan. The real figure is 9.5–12.0 s. `backend/app/observability.py` |

The last one is the worst kind: not an explanation that drifted from a measurement, but a
placeholder from a wireframe that became a design justification in two source files, four
times further from the truth than the real number. Nothing in the process would have found
it; it surfaced only because two files disagreed with each other.

The durable fix is not "check harder." It is to make the explanation **executable** —
which is what `floors.json`'s `headroom_rule` now is — so the file cannot disagree with
itself without a test failing.

---

## What it does

A question produces one HTTP stream. The frames arrive in a fixed order, and the ordering
is the whole design: the sources are on the wire before the first token, so the wait is
never empty.

```
event: message_start     {"message_id": "<uuid>", "model": "deterministic"}
event: citations         {"chunks": [Citation × ≤5], "mode": "fts"}
event: token             {"text": "Under "}
event: token             {"text": "the "}
…
event: message_end       {"finish_reason": "stop"}
```

Captured transcripts with real ids and real scores are in the contract
([§3](docs/api-contract.md#3-the-chat-flow-end-to-end),
[§4.2](docs/api-contract.md#42-events)), together with the two optional frames
(`generator_changed`, `error`) and every ordering guarantee. `score`'s unit depends on
`mode` — `ts_rank`, cosine similarity, or an RRF value — and is comparable within one
response and nowhere else; the contract says so at length because it is the trap a
frontend developer falls into first.

The retrieval is real: Postgres full-text search, pgvector cosine similarity, or
reciprocal-rank fusion of both, over a corpus the tenant owns. The gold set asks for the
same thing four ways on purpose. *"What must a controller do without undue delay and,
where feasible, not later than 72 hours after becoming aware of a personal data breach?"*
is the **verbatim** kind — Article 33(1) in the article's own words, and the floor below
which nothing else means anything. *"Who is the organisation that decides why and how
personal information is used?"* is the **synonym** kind: the word "controller" never
appears, and this is where the vector mode was supposed to gain most and does not. All
thirty questions, and what each kind is for, are in `backend/app/evals/goldset.py`.

**On latency, and why there is no single headline number here.** `fts` is one Postgres
query. `vector` and `hybrid` must embed the question first, which is a CPU-bound ONNX
inference on the request path (`backend/app/chat/embedding.py`), so they pay a cost per
question that `fts` does not. Generation is a second, larger cost and it depends on
which generator answered: the deterministic stub is instant, and Gemini measured
~2.2–2.5 s (flash-lite) and ~9.5–12.0 s (flash) for a long answer **from a developer
machine, not from the deploy box** (`frontend/components/panel/generator-selector.tsx`).
The demo page measures the citations-to-first-token interval live with
`performance.now()` on every turn; that is the number worth reading, and it is measured
where you are rather than quoted from where I was.

## Architecture

```
                    ┌──────────────┐
   browser ──HTTP──▶│  api         │──asyncpg──▶ Postgres 16 + pgvector
            SSE ◀───│  (FastAPI)   │              row-level security
                    └──────┬───────┘              role: lexloom_app
                           │ Redis (requirepass)
                    ┌──────▼───────┐
                    │  worker      │──asyncpg──▶ same database, same policies
                    │  (Celery)    │              role: lexloom_worker
                    └──────────────┘
                    ┌──────────────┐
                    │  beat        │  fires purge_expired_orgs hourly
                    └──────────────┘
```

One image, three processes. The API is async and never blocks; the worker is prefork and
does the CPU-bound work — parsing an 88-page PDF, chunking it, and computing 978
384-dimensional embeddings locally. Both connect as unprivileged roles and **refuse to
start** if the role they get can bypass row-level security. `beat` exists because a
schedule that only lives in configuration is indistinguishable, in a diff and in review,
from a schedule that works.

- **Isolation:** [ADR 0003](docs/adr/0003-tenant-isolation.md)
- **Why Celery and not arq:** [ADR 0001](docs/adr/0001-celery-over-arq.md)
- **Why SSE and not WebSocket:** [ADR 0002](docs/adr/0002-sse-over-websocket.md)
- **The scheduler that was declared and did not run:** [ADR 0004](docs/adr/0004-scheduled-purge.md),
  which supersedes one section of ADR 0001 rather than editing it — an ADR is the record
  of a decision at a date, not a live status page.

## Run it

No API keys, no accounts, no secrets to obtain. The signing keypair is generated in memory
at start-up and the default generator runs locally.

**1. Bring the stack up.**

```bash
cp .env.example .env
docker compose up -d --build
```

`.env.example` carries the Redis password the Compose file starts Redis with. It needs
one: Redis is published on host loopback, the intended deploy host is shared, and every
process on a shared host reaches loopback — "127.0.0.1 only" keeps a service off the
internet and away from nobody on the machine. If you have an older stack running, the
Redis container has to be recreated once.

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
# 0008 (head)
```

Running it with the container's own `DATABASE_URL` fails with
`permission denied for table alembic_version`, which is the design working: the
application role owns nothing and cannot change the schema it is confined by. Nothing in
Compose applies migrations for you.

**3. Check readiness.**

```bash
curl -fsS http://127.0.0.1:8100/health/ready
# {"status":"ready","checks":{"database":true,"redis":true}}
```

Readiness answers exactly one question — *are Postgres and Redis reachable from this
process* — and deliberately no more. It does **not** check that the schema exists: a
completely unmigrated database still returns `{"status":"ready"}`, and the first real
request then fails with a `500`. That is why step 2 comes first. Liveness is narrower
still and touches no dependency at all, so a transient database blip cannot get a healthy
process killed.

**4. Use it.** Walk the calls in
[docs/api-contract.md §3](docs/api-contract.md#3-the-chat-flow-end-to-end), or start with
one line:

```bash
curl -s -X POST http://127.0.0.1:8100/api/v1/auth/demo-login \
  -H 'Content-Type: application/json' -d '{"label":"Acme Legal"}'
```

Postgres is published on **127.0.0.1:5433**, not 5432 — a development host commonly runs
its own PostgreSQL on 5432, and Docker's port publish then loses to it in a way that is
invisible until `psql` reaches the wrong server.

Compose has no source bind mount, so a code or migration change needs
`docker compose build`. Without a rebuild, `alembic upgrade head` runs against the old
image, no-ops, and reports success.

Putting it on a real host — nginx, TLS, what `ENV=demo` makes mandatory, and the memory
budget for a shared box with 2.2 GB free — is [`deploy/README.md`](deploy/README.md), step
by step and with the failure modes attached.

### The demo page

```bash
cd frontend
npm ci
npm run dev          # http://127.0.0.1:3000, API proxied to :8100 from the same origin
npm run preview      # the same page, served from the static build that gets deployed
```

Chat on the left; on the right, a panel that reads the backend off the wire — the named
SSE events in arrival order, the measured interval between `citations` and the first
token, the caller's `org_id`, a cross-tenant probe made with a second tenant's real token,
the search-mode and generator selectors, live ingestion progress, the process's own
request and latency counters, and the evaluation table. Every figure on it is measured in
the browser; none of it is scripted.

`npm run build` produces `frontend/out/`, a directory of files. There is no Node process
in production: the same nginx that fronts the API serves them, which is also why the API
has no CORS middleware.

`npm audit` reports four moderate PostCSS advisories, all reached through build-time
tooling (`next@15.5.21`, `tailwindcss`, `vite`). They are stated here rather than left for
your tooling to find: in a static export PostCSS runs at build time and ships zero bytes
to the browser, so the runtime exposure of this page is nil. The finding is real; the risk
it describes is not present in what gets deployed.

### Tests

```bash
cd backend
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q          # 364 passed, 1 skipped
```

```bash
cd frontend && npm test      # 22 passed, 6 files
```

The backend suite needs the Compose Postgres and Redis running. It is not a mock suite: it
opens raw connections as `lexloom_app`, attempts forbidden operations, drives a real
uvicorn over a real socket, drops that socket mid-answer, provokes a genuine Postgres
deadlock, and spawns real `celery beat` and `worker` subprocesses to prove the scheduled
purge actually deletes rows. The one skipped test is an opt-in live call to Gemini, which
needs a key CI does not have. On a cold embedding-model cache the suite refuses in about a
second naming the cause and the fix, rather than downloading 90 MB inside a fixture and
failing thirty seconds later with `uvicorn never became live`.

CI runs four jobs against service containers: the suite, the retrieval gate, the frontend
build and tests, and a Compose smoke test that asserts every service is still running
rather than only that the API answers.

## What this demonstrates

- **Retrieval you can argue with.** Thirty questions with graded judgements and a written
  reason for each, an authorship procedure designed to stop the gold set flattering the
  system, three modes measured against it, and a CI gate whose thresholds are derived by
  a machine-readable rule instead of chosen. Including the results that went the wrong
  way.
- **Tenant isolation the database enforces.** Row-level security keyed on a
  transaction-local setting, unprivileged application roles, and a start-up guard that
  refuses to boot as a role which could bypass any of it — including one that is merely a
  *member* of such a role. Cross-tenant requests answer `404`, and the body is
  byte-identical to the `404` for a resource that does not exist anywhere. The isolation
  test is written so that an *empty* result cannot pass it: absence proves nothing, so the
  test asserts the other tenant's own rows come back too.
- **Streaming that behaves under a real client.** Named SSE events, citations before
  tokens, and disconnect handling verified by hanging up a raw socket mid-answer rather
  than by cancelling a task in a unit test — those turn out to be two different things.
- **An error contract with no holes.** Every response, including routing `404`s, `405`s,
  validation failures and unhandled exceptions, is RFC 9457 `application/problem+json`
  with a stable `code` and a `request_id` that joins to the logs. Authentication failures
  never say *why*, and a failed ingestion returns a code rather than the exception's text.
- **Documentation a frontend developer can work from.** Both stream-consumption paths,
  executed; every error code, captured; and the traps written down — CRLF framing,
  `EventSource` reconnecting after a *successful* stream, `score` being comparable only
  within one response and meaningless across modes.
- **Decisions with their costs attached.** Four ADRs. Three of them carry a section
  headed *What I got wrong and what it cost* — a table everyone believed was protected and
  was not; guards that reported success against the very thing they existed to catch; a
  response sent before its own transaction committed; a test that could not fail — each
  written up with the measurement that exposed it and naming the code that now holds the
  property. The fourth ADR exists because one of those mistakes was still live: it
  supersedes a section of ADR 0001 instead of quietly editing it.

## Honest limits

- **No live demo URL.** Everything above runs locally. The deployment is written down
  rather than performed — a shared host, nginx, TLS and the memory budget it has to fit
  inside are in [`deploy/README.md`](deploy/README.md).
- **The default generator is not a language model.** `model: "deterministic"` is a canned
  generator behind the provider interface. Retrieval, streaming, cancellation, grounded
  prompt assembly and citations are all real; the *words* are not an answer to your
  question. A real provider is selectable — Gemini's free tier, two model choices, chosen
  per message — and `GET /api/v1/generators` lists which choices would actually work right
  now, so a clone with no API key still lists a working chat instead of offering a button
  that errors. With a Gemini choice and no key configured the request fails outright with
  `422 generator_unavailable`, never a generic `500` and never a silent fallback to the
  stub.
- **The corpus is one document.** The catalogue is a literal dict with one entry, the
  public GDPR text; there is no user file upload, so `source_key` can never become a path.
  A tenant that ingests it holds 1008 chunks: the 978 parsed from the PDF plus the 30
  seeded excerpts.
- **The frontend is a demonstration page, not a product UI.** One conversation, no
  history, no document management, no design system. The API is the deliverable.
- **You cannot read a conversation back.** Four `GET` routes return data
  (`/api/v1/status`, `/api/v1/generators`, `/api/v1/ingest/status`, `/api/v1/evals`), and
  [§1 of the contract](docs/api-contract.md#1-the-whole-surface) works out why none of
  them is an exception to that. There is no conversation list, no message list, no
  `GET /conversations/{id}`: a page reload loses the conversation from the UI while the
  rows survive in the database until the tenant expires.
- **The evals floors are provisional.** They gate the build today, and they were set
  before a human read the thirty questions they derive from. The marker stays until
  somebody does.
- **The retrieval numbers are low in absolute terms.** They are published anyway, with
  their ceilings, because a weak honest number is worth more than a strong indefensible
  one in a repository whose purpose is to be examined.
- **`INGEST_ORG_CAP` is 8,** and an organisation holding a corpus consumes a slot until
  its TTL lapses. One visitor filling all eight would have switched the headline feature
  off for everyone for 24 hours; the TTL of a tenant that *starts* an ingest is now
  shortened to 2 hours, which turns a one-shot outage into one that has to be continuously
  re-mounted. **The residual is real and stated rather than papered over:** a sustained
  attacker on one address can still hold the cap.
- **The demo-login limit is per address, not global.** 30 tenants per address per hour,
  plus 16 concurrent streams for the process and 4 per tenant, refused with `429`/`503`
  and a `Retry-After`. What that does not bound is a caller spread across many addresses.
- **No CORS.** A preflight returns `405`. The frontend must be same-origin behind the
  reverse proxy.
- **No `Idempotency-Key`,** anywhere. A retried `POST` creates a second row.
- **`FORCE ROW LEVEL SECURITY` is set on every tenant table and is currently inert,**
  because the migration role is the bootstrap superuser. Explained in full, with the
  measurement, in
  [ADR 0003](docs/adr/0003-tenant-isolation.md#2-force-row-level-security-is-inert-in-this-deployment).
- **Two concurrent streams on one message both succeed** and leave two assistant rows.
  The `409` is a replay guard, not a lock.
- **`(source_key, chunk_ordinal)` uniqueness is an application invariant, not a database
  constraint.** One ingest per organisation is what holds it, and the total ordering the
  eval gate's stability depends on rests on that. The durable fix is a partial unique
  index; it is a schema change and it has not been made.
- **This milestone has never been through CI.** Nothing in it has been pushed yet, so the
  first run happens with a human watching — the things it can find that a local run cannot
  (the model-cache key, whether the runner has what the extractor needs, the Compose
  service literal) are worth watching live rather than fixing blind.

## Demo hardening

The demo is meant to be handed to a stranger, which imposes constraints an internal tool
would not have.

- **A tenant per visitor.** `POST /api/v1/auth/demo-login` provisions a throwaway
  organisation. A project whose headline claim is per-tenant isolation cannot seat every
  visitor in the same tenant. Open two browsers and they get different `org_id`s and
  cannot see each other.
- **The corpus is copied, not shared.** Each demo tenant owns its own 30 seeded chunks,
  and its own 978 more if it ingests. The cheaper design — one corpus organisation with a
  read-through policy exception — would punch a hole in the exact property the project
  exists to demonstrate.
- **No key material in the repository.** The RSA keypair is generated in memory at
  start-up. A restart rotates the `kid` and invalidates every outstanding token; that is
  the trade for having nothing to leak. The dev identity provider refuses to construct
  itself under `ENV=production`. A Gemini key, where one is configured, travels only as an
  `x-goog-api-key` header and appears in no URL, log, exception or status payload.
- **Ephemeral data.** Demo tenants carry a TTL and an hourly `beat` fires the purge, which
  removes the organisation *and its children* in one transaction. The race between the
  purge and an in-flight ingestion is closed with `FOR KEY SHARE` rather than narrowed —
  four separate reviews re-derived that argument and each honestly called it a residual;
  none had tried the one-token fix, because re-deriving an argument is not the same act as
  asking whether it can be made unnecessary.
- **Failures say nothing useful to a prober.** A rejected token returns one fixed string
  whatever the reason — expired, wrong audience, unknown key, wrong issuer. The reason goes
  to the log. Cross-tenant access returns `404`, never `403`. `GET /api/v1/status`
  publishes no stream occupancy and nothing per-organisation, because the limit and how
  much of it is spent are both information about the ceiling.
- **Tracebacks are logged as strings, not structured frames.** Frame-local logging copies
  every local variable, and in an ingestion path those hold tenant document text — an
  observability change that quietly exports customer data.
- **Bounded containers.** Every service sets `mem_limit` equal to `memswap_limit`, because
  Docker reads `memswap_limit` as memory *plus* swap and the two being equal is what means
  "no swap for this container": 384 + 64 + 450 + 650 + 96 = **1644 MB** against a box with
  ~2.2 GB free, 2 vCPUs and no swap of its own. An unbounded process there does not
  degrade politely; it thrashes the host and then gets somebody killed, and the kernel
  picks who.

## Repository

```
backend/app/        api/v1/  auth/  chat/  db/  evals/  ingest/  worker/    application
backend/alembic/    0001…0008                             schema, roles, policies, functions
backend/tests/      real Postgres, real uvicorn, real celery subprocesses
backend/seed/       30 GDPR excerpts, sourced from EUR-Lex with the reuse basis recorded
backend/app/ingest/sources/   the 88-page GDPR PDF, with its sha256 and provenance
frontend/           Next.js static export: the demo page, its backend panel, and vitest
docs/               api-contract.md, errors.md, adr/0001…0004
deploy/             README.md — the runbook; nginx vhost and proxy snippet; role bootstrap
```

Everything is English: prose, code, comments and commit history. The commit log is meant
to be readable — each message says what changed and why, and several of them say what was
wrong before.
