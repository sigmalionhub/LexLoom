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
# the repository; note that nothing in docker-compose.yml runs `celery beat`
# yet, so on the current deployment this schedule is intent, not behaviour.
celery_app.conf.beat_schedule = {
    "purge-expired-demo-orgs": {
        "task": "lexloom.purge_expired_orgs",
        # Hourly. The TTL is 24h, so the granularity that matters is "well
        # under a day"; running it more often would only add wakeups on a
        # 2 vCPU box shared with other applications.
        "schedule": 3600.0,
    }
}

celery_app.autodiscover_tasks(["app.worker"])
