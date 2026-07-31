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


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    """Each test gets its own event loop; the engine's pool must not outlive it.

    A connection created on a previous test's loop and handed to the next one
    fails in ways that point at the wrong code entirely.
    """
    yield
    from app.db.engine import engine

    await engine.dispose()
