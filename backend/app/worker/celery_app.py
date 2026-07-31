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
celery_app.autodiscover_tasks(["app.worker"])
