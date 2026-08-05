import math
import os

from celery import Celery

from app.config import get_settings
from app.redis_auth import assert_redis_requires_a_password

settings = get_settings()

# At import, which is where this process's other fatal configuration errors are
# already raised (`_parse_purge_schedule_seconds` below). The worker and beat
# hold the same broker as the API, so a public deployment whose Redis takes
# anonymous connections must not have a worker either — a stack that keeps
# consuming a queue anyone on the box can write to is worse than one that is
# visibly down.
assert_redis_requires_a_password(settings)

celery_app = Celery("lexloom", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    result_expires=3600,
    task_soft_time_limit=300,
    task_time_limit=360,
)


def _parse_purge_schedule_seconds(raw: str) -> float:
    """Parse and validate `PURGE_EXPIRED_ORGS_SCHEDULE_SECONDS`.

    Anything that is not a strictly positive, finite number is rejected here —
    at import time, so a bad value crash-loops the container with a clear
    message — rather than handed to Celery. `celery.schedules.schedule`
    treats `0` or a negative `run_every` as "always due": verified directly,
    `schedule(run_every=0.0).is_due(...)` and `schedule(run_every=-5.0).is_due(...)`
    both report `is_due=True` on every check. A bad value here would therefore
    not crash `beat` — it would make it call `purge_expired_orgs` in a tight
    loop against a Postgres shared with other applications on this box, which
    is a worse failure than a crash-loop, not a milder one.

    `math.isfinite` is checked as well as `<= 0`: NaN compares `False` to
    everything, including `<= 0`, so a NaN input would otherwise slip past a
    bare positivity check silently and reach Celery anyway.

    An empty string cannot reach here from `docker-compose.yml` —
    `${PURGE_EXPIRED_ORGS_SCHEDULE_SECONDS:-3600}` treats an empty host value
    the same as unset and substitutes `3600` — but `float("")` still raises
    here if something ever calls this directly, bypassing Compose.
    """
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"PURGE_EXPIRED_ORGS_SCHEDULE_SECONDS={raw!r} is not a number") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(
            f"PURGE_EXPIRED_ORGS_SCHEDULE_SECONDS={raw!r} must be a strictly positive, "
            "finite number of seconds; 0 or a negative interval makes Celery treat the "
            "schedule as always due, firing the purge in a tight loop"
        )
    return value


# 3600 unless overridden, which nothing in production does. Read with a plain
# `os.environ.get`, not through `Settings`, so this file keeps not depending
# on `app.config` for anything beyond the broker URL it already needed.
PURGE_SCHEDULE_SECONDS = _parse_purge_schedule_seconds(
    os.environ.get("PURGE_EXPIRED_ORGS_SCHEDULE_SECONDS", "3600")
)

# Demo tenants are only ephemeral if something actually removes them. Declared
# here rather than passed on a command line so the interval is reviewable in
# the repository; `docker-compose.yml` runs it on a dedicated `beat` service,
# not on the worker itself (see the comment there for why: `-B` on the worker
# ties the schedule to one process's lifetime and duplicates it the moment a
# second worker exists).
celery_app.conf.beat_schedule = {
    "purge-expired-demo-orgs": {
        "task": "lexloom.purge_expired_orgs",
        # Hourly by default. The TTL is 24h, so the granularity that matters
        # is "well under a day"; running it more often would only add wakeups
        # on a 2 vCPU box shared with other applications. A test overrides
        # PURGE_SCHEDULE_SECONDS to prove the purge fires without waiting an
        # hour for it.
        "schedule": PURGE_SCHEDULE_SECONDS,
    }
}

celery_app.autodiscover_tasks(["app.worker"])
