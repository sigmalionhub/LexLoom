# ADR 0001 — Celery for background work, not arq

**Status:** accepted
**Date:** 2026-08-01

## Context

The backlog for this service is CPU-bound: parse an uploaded PDF, chunk it, compute
384-dimensional embeddings locally, write them back. That work cannot run in the API
process — a synchronous ONNX inference call blocks the event loop and every
concurrent SSE stream on it stops mid-token. So there is a second process, and
something has to get work to it.

The realistic options were Celery, arq, Dramatiq and "just use `BackgroundTasks`".

`BackgroundTasks` was eliminated first and is worth naming because it is what most
FastAPI examples reach for. It runs in the API process. That is the exact thing this
architecture exists to avoid, and it also means a task dies with the request that
started it: an ingestion job disappears when the client hangs up.

arq is the natural async-native choice next to an async FastAPI app, and the shape is
attractive: same asyncio idioms, no protocol translation, a much smaller surface.

## Decision

**Celery with Redis as both broker and result backend, prefork pool, concurrency 1,
prefetch multiplier 1, `max-tasks-per-child=50`.**

The deciding argument was not throughput. It was:

1. **The work is CPU-bound, and the concurrency model has to match.** arq's model is a
   single event loop running coroutines. An ONNX inference call is not a coroutine —
   it is a blocking C call — so under arq every embedding batch either blocks the
   whole worker's loop or has to be hand-dispatched to a thread pool, which is
   Celery's prefork model rebuilt by hand and worse. Celery's prefork pool gives
   process isolation for free, and `max-tasks-per-child` bounds the memory a leaking
   native library can accumulate. That bound matters concretely: the deployment target
   has 2.2 GB of free RAM and **no swap**, so a worker that grows without a ceiling
   does not degrade, it takes the box down.

2. **`task_acks_late=True` with `task_reject_on_worker_lost=True`.** A task is
   acknowledged after it completes, not when it is delivered. If the worker is killed
   mid-parse — which on a box with no swap means the OOM killer, not a crash — the job
   returns to the queue instead of vanishing. This is the property that makes "202
   Accepted" an honest answer.

3. **Operational legibility.** `celery -A app.worker.celery_app inspect registered`
   answers "is the worker actually consuming from the broker I think it is" in one
   command, against the live system:

   ```console
   $ docker compose exec worker celery -A app.worker.celery_app inspect registered
   ->  celery@8ac989bd7ade: OK
       * lexloom.ping
       * lexloom.purge_expired_orgs

   1 node online.
   ```

The cost accepted: Celery is synchronous, so every task that touches the database
opens its own event loop with `asyncio.run`. That is genuinely uglier than arq would
be, and it is the source of the two mistakes below.

### What the worker is not allowed to be

The worker connects as `lexloom_worker` and is subject to **the same row-level
security as the API**. It has no `BYPASSRLS`, and the same start-up guard that the
API runs also runs in each prefork child, after the fork:

```python
@worker_process_init.connect
def on_worker_process_init(**_):
    engine.sync_engine.dispose(close=False)   # drop inherited sockets, do not close them
    asyncio.run(_check())                     # refuses to boot on a privileged role
```

Granting the worker `BYPASSRLS` "because it is trusted" is precisely how a parser
vulnerability in an uploaded PDF becomes a cross-tenant breach. `org_id` travels in
the task payload and the task opens its transaction with the same tenant context the
API uses.

`dispose(close=False)` is load-bearing. A fork copies file descriptors, so the child
inherits the parent's live Postgres sockets. Closing them from the child corrupts the
wire protocol for the parent too; dropping them without closing is the only correct
move. The same reasoning is why the M2 embedding model will be constructed in this
hook rather than at module import: a fork copies a thread pool's bookkeeping but not
its threads, so an `InferenceSession` built in the parent hangs in the child with no
error and no traceback.

## Consequences

- Two processes from one image. The API never blocks; the worker never serves HTTP.
- Redis is a hard dependency for background work and is already present as a cache.
- The result backend keys are not tenant-scoped. Task ids are never exposed to
  clients, so a caller cannot address one — but that is a *convention*, not a control,
  and it is recorded as such in [ADR 0003](0003-tenant-isolation.md).
- Two tasks exist today: `lexloom.ping`, a smoke task that proves the API can reach
  the broker and a worker consumes from it, and `lexloom.purge_expired_orgs`, which
  reclaims demo tenants past their TTL.

---

## What I got wrong and what it cost

### The scheduler is declared and does not run

`celery_app.conf.beat_schedule` names `purge-expired-demo-orgs` on an hourly
interval. Nothing runs `celery beat`. The Compose worker service is:

```
celery -A app.worker.celery_app worker --loglevel=info --concurrency=1 …
```

No `--beat`, and no separate beat container. So the 24-hour tenant TTL — a promise
the demo makes to every visitor about their data — **is currently enforced by
nothing**. The task works; invoking it by hand returns a count:

```console
$ docker compose exec worker python -c "from app.worker.tasks import purge_expired_orgs; print(purge_expired_orgs.delay().get(timeout=60))"
0
```

Zero because nothing has expired yet, not because the purge is broken. But a schedule
that exists in configuration and nowhere in the process tree is the most expensive
kind of wrong: it reads as done in the code, in review, and in this document, and it
is discoverable only by looking at what is actually running.

The cost so far is contained only because nothing has aged out yet, and that is all:

```
 all_orgs | ttl_bearing | already_expired    (development database, 2026-08-01)
     1605 |         129 |               0
```

Only the middle column is this section's subject. The other 1476 organisations carry
`expires_at IS NULL` — the permanent seed corpus and everything the test suite creates —
and the purge is not meant to touch them; they are counted here because ADR 0003 quotes
the total for a different argument and two undisambiguated numbers read as a
contradiction. Reproduce these rather than compare them: every demo login and every test
run moves them.

`already_expired` is the one that matters, and it is zero because the oldest TTL-bearing
tenant is younger than a day. That is a fact with a shelf life, not a mitigation — the
first tenant to age out is the first one that is never collected, and 129 of them are
waiting to. What it cost in judgement is larger: I wrote a schedule and believed
the feature was finished, and the gap was found by listing running services, not by
reading the code that declares it. The code now carries the disclaimer in a comment
rather than looking functional, which is the minimum honest state, but the fix is a
deployment change and it is still open.

### The obvious retry predicate would have retried the wrong things

The purge locks organisations and then their children. A request handler that updates
a message and then its own organisation row takes those locks in the opposite order,
and Postgres resolves the cycle by killing one side — which can be the purge. Because
the purge is one statement, losing it loses the whole run, and the next attempt is an
hour away.

The obvious fix is the one Celery documents:

```python
@celery_app.task(autoretry_for=(DBAPIError,), retry_backoff=True)
```

That is wrong, and reading it does not reveal why. I provoked a real deadlock rather
than reasoning about it, and found that SQLAlchemy surfaces an asyncpg deadlock as a
**bare `DBAPIError`** — not `OperationalError`, not a deadlock-specific subclass —
with the SQLSTATE reachable only through `.orig`:

```
DBAPIError, .orig is asyncpg.Error, .orig.sqlstate == '40P01'
```

So `autoretry_for=(DBAPIError,)` matches every database failure there is. A permission
denial (`42501`) and a syntax error would be retried three times with backoff before
finally failing, which turns an instant, legible error into a delayed, confusing one —
and does it on the security-relevant path, where `42501` is the signal that a policy
just refused something. The shipped predicate matches the SQLSTATE and nothing else.

What it cost: the version I would have shipped by following the documentation was
tested and green, because the test provoked a deadlock and a deadlock *is* retried.
The bug was entirely in the cases the test did not raise.

### An engine disposed outside its own event loop

`asyncio.run` closes the loop it created when it returns. The first shape of the
post-fork hook disposed the SQLAlchemy engine *after* that call, so a pooled
connection outlived the loop it was bound to. It does not fail there. It fails in the
next task, in a different module, with a traceback pointing at code that is correct.

Both `on_worker_process_init` and `purge_expired_orgs` now dispose inside their own
`asyncio.run`, with the reason written next to each. The general shape of the mistake
— a module-level async engine singleton is bound to the loop that first used it — also
surfaced in the test suite, where each test gets its own loop and multiple
database-touching tests in one module produced cross-event-loop pooling warnings. That
is the seam Celery's synchronous model creates, and it is the honest price of the
choice made above. arq would not have had it.
