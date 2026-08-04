import os

from celery import Celery

from app.config import get_settings

settings = get_settings()

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
        # on a 2 vCPU box shared with other applications. Read from the
        # environment with a plain `os.environ.get`, not through `Settings`,
        # so this file keeps not depending on `app.config` for anything beyond
        # the broker URL it already needed. Nothing sets the variable in
        # production, so 3600 is what actually runs; a test overrides it to
        # prove the purge fires without waiting an hour for it.
        "schedule": float(os.environ.get("PURGE_EXPIRED_ORGS_SCHEDULE_SECONDS", "3600")),
    }
}

celery_app.autodiscover_tasks(["app.worker"])
