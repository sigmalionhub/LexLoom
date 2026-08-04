"""A real `celery beat` fires the purge into a real `celery worker`.

`test_compose_environment.py` proves the schedule is *built* correctly: the
default is hourly, a bad `PURGE_EXPIRED_ORGS_SCHEDULE_SECONDS` is rejected,
and the task name in `beat_schedule` is the one actually registered. None of
that proves a running `beat` process reads the schedule and a running
`worker` process executes what it sends — the two things
`docker-compose.yml`'s `beat` and `worker` services do for real. This is
that proof, run without a live `docker compose` stack: `beat` and `worker`
are spawned as plain OS subprocesses against the same Postgres and Redis this
test session already talks to, which is exactly what makes it possible in CI
without Docker-in-Docker — CI already brings up service containers for
those. Same pattern as `live_server` in test_stream_cancellation.py, which
spawns a real `uvicorn` the same way.

Deliberately not exercising `docker-compose.yml` itself (its `beat` command,
its `--schedule=/tmp/...` flag, its `depends_on`): that is what the
compose-smoke job in `ci.yml` and a real `docker compose up` are for, and
they were run by hand for this task (see the task-32 report). This test is
scoped to one thing only: does a process that reads `beat_schedule` the way
`beat` does, running against a process that executes tasks the way `worker`
does, actually purge an expired organisation and all four of its
`document_chunks`/`conversations`/`messages`/`organizations` rows.
"""

import asyncio
import os
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from app.auth.demo import provision_demo_org
from app.db.session import tenant_session

_BACKEND_ROOT = Path(__file__).resolve().parents[1]

# Generous relative to the 2s schedule the fixture below sets, not relative to
# how long a purge actually takes (well under a second): it has to cover
# Python interpreter start, Celery app import, and a Redis/Postgres
# connection for two separate processes on a loaded CI runner, not just the
# wait itself.
POLL_TIMEOUT_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 1.0

# Full statements, not an f-string built from a table/column pair: the
# variable-looking construction is what ruff's S608 flags, and a literal per
# table is what CHILD_COUNT_SQL in test_demo_orgs.py already does for the
# same reason.
_ROW_COUNT_SQL = {
    "organizations": "SELECT count(*) FROM organizations WHERE id = :i",
    "conversations": "SELECT count(*) FROM conversations WHERE org_id = :i",
    "messages": "SELECT count(*) FROM messages WHERE org_id = :i",
    "document_chunks": "SELECT count(*) FROM document_chunks WHERE org_id = :i",
}


async def _row_counts(org_id: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    async with tenant_session(org_id) as session:
        for table, sql in _ROW_COUNT_SQL.items():
            result = await session.execute(text(sql), {"i": org_id})
            counts[table] = result.scalar()
    return counts


@pytest.fixture
def beat_and_worker(tmp_path):
    """Run real `beat` and `worker` processes with a 2-second purge schedule.

    A fixture rather than inline `subprocess.Popen` in the (async) test body:
    ruff's ASYNC220 rightly objects to spawning blocking subprocesses inside
    `async def`, and a plain fixture sidesteps the question entirely rather
    than silencing it.

    `DATABASE_URL` is switched to the worker role: migration 0004 grants
    EXECUTE on `purge_expired_orgs()` to `lexloom_worker` only, exactly as
    `docker-compose.yml`'s `beat` and `worker` services connect as
    `lexloom_worker` and not `lexloom_app`.
    """
    from conftest import WORKER_DATABASE_URL

    env = {
        **os.environ,
        "DATABASE_URL": WORKER_DATABASE_URL,
        "PURGE_EXPIRED_ORGS_SCHEDULE_SECONDS": "2",
    }
    common_args = {
        "cwd": _BACKEND_ROOT,
        "env": env,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    beat = subprocess.Popen(  # noqa: S603 - fixed argv, no shell, test-only
        [
            sys.executable,
            "-m",
            "celery",
            "-A",
            "app.worker.celery_app",
            "beat",
            "--loglevel=info",
            f"--schedule={tmp_path / 'celerybeat-schedule'}",
        ],
        **common_args,
    )
    worker = subprocess.Popen(  # noqa: S603 - fixed argv, no shell, test-only
        [
            sys.executable,
            "-m",
            "celery",
            "-A",
            "app.worker.celery_app",
            "worker",
            "--loglevel=info",
            "--concurrency=1",
            "--pool=solo",
        ],
        **common_args,
    )
    try:
        yield
    finally:
        for proc in (beat, worker):
            proc.terminate()
        for proc in (beat, worker):
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)


@pytest.mark.timeout(90)
async def test_a_real_beat_process_drives_a_real_worker_to_purge(created_orgs, beat_and_worker):
    """Seed an already-expired organisation with a full set of child rows,
    then wait for the `beat_and_worker` fixture's live processes to purge it
    — the same assertion the task-32 report's manual `docker compose` run
    makes, now committed and run on every CI push.
    """
    org = await provision_demo_org("celery beat wiring proof")
    org_id = org.id
    created_orgs.append(org_id)  # safety net if the purge never fires

    async with tenant_session(org_id) as session:
        await session.execute(
            text("UPDATE organizations SET expires_at = :x WHERE id = :i"),
            {"x": datetime.now(UTC) - timedelta(hours=1), "i": org_id},
        )
        conversation_id = str(uuid.uuid4())
        await session.execute(
            text("INSERT INTO conversations (id, org_id, title) VALUES (:c, :o, 'proof')"),
            {"c": conversation_id, "o": org_id},
        )
        await session.execute(
            text(
                "INSERT INTO messages (id, org_id, conversation_id, role, content) "
                "VALUES (:m, :o, :c, 'user', 'proof')"
            ),
            {"m": str(uuid.uuid4()), "o": org_id, "c": conversation_id},
        )

    before = await _row_counts(org_id)
    assert before == {
        "organizations": 1,
        "conversations": 1,
        "messages": 1,
        # provision_demo_org's own copy of the seed corpus.
        "document_chunks": 30,
    }, f"seed did not produce the expected rows: {before}"

    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    after = before
    while time.monotonic() < deadline:
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        after = await _row_counts(org_id)
        if after["organizations"] == 0:
            break

    assert after == {
        "organizations": 0,
        "conversations": 0,
        "messages": 0,
        "document_chunks": 0,
    }, (
        f"a live beat+worker pair did not purge the expired organisation within "
        f"{POLL_TIMEOUT_SECONDS}s; last observed counts={after}"
    )
