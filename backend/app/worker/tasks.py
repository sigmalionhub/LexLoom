from app.worker import signals  # noqa: F401  (registers process-init handler)
from app.worker.celery_app import celery_app


@celery_app.task(name="lexloom.ping")
def ping(payload: str) -> dict[str, str]:
    """Smoke task proving the API can reach the broker and a worker consumes it."""
    return {"echo": payload, "worker": "lexloom"}
