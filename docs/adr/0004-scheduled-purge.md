# ADR 0004 — The demo-org purge is scheduled and runs

**Status:** accepted; amended once on 2026-08-06 — the residual gap under "What is still not covered" is closed. See [Amendments](#amendments).
**Date:** 2026-08-05
**Supersedes:** [ADR 0001](0001-celery-over-arq.md) §"The scheduler is declared and does not run"

## Context

[ADR 0001](0001-celery-over-arq.md) chose Celery over arq, and then, under *"What I got
wrong and what it cost"*, recorded a defect it had shipped:

> `celery_app.conf.beat_schedule` names `purge-expired-demo-orgs` on an hourly interval.
> Nothing runs `celery beat`. […] So the 24-hour tenant TTL — a promise the demo makes to
> every visitor about their data — **is currently enforced by nothing**.

That was true on 2026-08-01 and it is the reason this ADR exists. It is no longer true.
ADR 0001's body is deliberately left as written: an ADR is the record of a decision at a
date, not a live status page, and rewriting it would delete the evidence of the mistake
along with the mistake. This document is the amendment.

The gap was never in the task. `purge_expired_orgs` worked and returned a count when
invoked by hand; ADR 0001 shows that transcript. The gap was that the schedule existed
only in configuration, and configuration that nothing reads is indistinguishable, in a
diff and in review, from configuration that works. It was found by listing running
services, not by reading code.

## Decision

**A dedicated `beat` service in Compose, alongside `worker`, running
`celery -A app.worker.celery_app beat`.**

`docker-compose.yml`:

```yaml
  beat:
    build: ./backend
    command: celery -A app.worker.celery_app beat --loglevel=info --schedule=/tmp/celerybeat-schedule
```

Three things about that command are decisions rather than boilerplate.

**A separate service, not `worker --beat`.** `--beat` embeds the scheduler in a worker
process, which is documented as unsuitable for production and, more concretely here,
couples the two lifecycles: `--max-tasks-per-child=50` recycles the worker's child
processes, and a scheduler sharing that fate is a scheduler whose next tick depends on
how much ingestion traffic the box saw. A separate container also makes the original
failure impossible to repeat in the same shape — the scheduler is now something that
appears, or does not appear, in `docker ps`.

**`--schedule=/tmp/celerybeat-schedule`.** Beat writes its last-run bookkeeping to a
file, by default in the working directory. The image's `WORKDIR /app` is created by the
`WORKDIR` instruction, which runs as root before the `--chown=lexloom:lexloom COPY`, so
the directory stays root-owned while the files in it do not. `beat` runs as `lexloom`
and is the only service that writes into its working directory at runtime, so without
this flag it exits at startup with a bare "Permission denied". `/tmp` is not a volume,
so the file does not survive a restart; the cost of that is beat re-scheduling from
"now" rather than from its last tick, which is one wakeup, not a correctness problem.

**One `beat`, not one per replica.** Two schedulers means two purges per interval. The
purge is idempotent — it deletes what has expired — so a double fire is wasteful rather
than wrong, but the service is written as a singleton and should stay one.

## Consequences

The 24-hour tenant TTL is now enforced by a process rather than by a promise. The
schedule itself is unchanged: hourly by default, overridable through
`PURGE_EXPIRED_ORGS_SCHEDULE_SECONDS`, which rejects zero, negative, non-numeric and
non-finite values (`_parse_purge_schedule_seconds`) — a non-positive `run_every` makes
`celery.schedules.schedule` always due, and would spin the purge in a tight loop against
a Postgres shared with other applications on this box.

**What holds it, and at which level.** Three tests, and it is worth being exact about
which one proves what, because the original defect was precisely a gap *between* levels
that each looked covered:

- `backend/tests/test_compose_environment.py` proves the schedule is **built** correctly
  — the hourly default, the rejection of bad overrides, and that the name in
  `beat_schedule` is a task actually registered on the app. None of that requires a
  scheduler to exist.
- `backend/tests/test_celery_beat.py` proves a **running** `beat` reads the schedule and
  a **running** `worker` executes what it sends: both are spawned as ordinary OS
  subprocesses against the same Postgres and Redis the test session already uses, and
  the assertion is that an expired organisation and all of its
  `document_chunks` / `conversations` / `messages` / `organizations` rows are gone. This
  is the level at which the original defect lived, and it is the level that had no test.
- The compose-smoke job in `ci.yml` and a real `docker compose up` cover
  `docker-compose.yml`'s own `beat` command, its `--schedule` flag and its `depends_on`.
  `test_celery_beat.py` deliberately does not, so that it can run in CI without
  Docker-in-Docker.

**What is still not covered.** **This paragraph records the state when this ADR was
written; the gap was closed later the same day — see the amendment below.** No test asserts
that a `beat` service exists in
`docker-compose.yml`. Deleting that service would leave all three levels above green and
reproduce the exact defect this ADR closes, discoverable only by looking at what is
running — which is how it was found the first time. That is a real residual gap, stated
here rather than quietly closed, because the honest fix is a compose-level assertion and
it belongs with whoever next touches the deployment.

ADR 0001's own closing judgement is left standing and is worth repeating, since it is
what this document is evidence for: *I wrote a schedule and believed the feature was
finished.* The correction is not that the schedule was wrong. It is that "declared" and
"running" are two different claims, and only one of them was ever checked.

---

## Amendments

### 2026-08-06 — The residual gap named above is closed

The "What is still not covered" paragraph says *"No test asserts that a `beat` service
exists in `docker-compose.yml`"* and hands the fix to whoever next touches the deployment.
That paragraph is left as written — it records the state on 2026-08-05 — and the fix landed
the same day, in commit `67dcb93`.

`backend/tests/test_compose_environment.py:220`,
`test_a_service_in_compose_actually_runs_celery_beat`, loads `docker-compose.yml` and
requires at least one service whose `command` contains the word `beat`. Its failure message
cites this ADR by number, so a run that deletes the service reads as the defect returning
rather than as an unexplained red.

It matches on the argv, not on the service's *name*: renaming `beat` to something else is
allowed, removing the `celery … beat` process is not. The name is a label; the process is
what fires the schedule, and the label was never the thing that was missing.

The residual that remains is narrower than the one this paragraph replaced. The test proves
a service is *declared* to run `beat` in the committed Compose file. That a scheduler is
actually running on the deployment box is still established by looking — the compose-smoke
job in `ci.yml` compares the running set against `api beat postgres redis worker`, which is
the closest anything here comes to checking it, and it checks CI's stack rather than the
deployment's.
