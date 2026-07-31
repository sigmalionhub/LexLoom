import os

import pytest

os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://lexloom_app:app@localhost:5433/lexloom"
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("ENV", "ci")

from app.worker.celery_app import celery_app  # noqa: E402


@pytest.fixture
def celery_eager():
    celery_app.conf.task_always_eager = True
    yield
    celery_app.conf.task_always_eager = False
