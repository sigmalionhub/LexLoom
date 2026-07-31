import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.config import get_settings
from app.db.engine import engine

router = APIRouter(tags=["health"])
logger = structlog.get_logger(__name__)


async def _database_ok() -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        # Log it. Without this, bad credentials, a network partition and
        # "still warming up" are indistinguishable from the outside — the
        # probe says 503 and leaves nobody anything to act on.
        logger.warning("readiness_database_check_failed", exc_info=True)
        return False


async def _redis_ok() -> bool:
    try:
        # `async with` closes the client on both paths. Closing it only after a
        # successful ping leaks a connection pool every time Redis is down —
        # and a readiness probe is polled continuously, so the leak compounds
        # exactly when the box is already unhealthy.
        async with aioredis.from_url(get_settings().redis_url) as client:
            await client.ping()
        return True
    except Exception:
        logger.warning("readiness_redis_check_failed", exc_info=True)
        return False


@router.get("/health/live")
async def liveness() -> dict[str, str]:
    """Process is running. Never touches dependencies.

    Conflating this with readiness is the classic cause of a supervisor
    killing a healthy process during a transient database blip.
    """
    return {"status": "alive"}


@router.get("/health/ready")
async def readiness(response: Response) -> dict[str, object]:
    """Dependencies are reachable."""
    checks: dict[str, bool] = {"database": await _database_ok(), "redis": await _redis_ok()}
    response.status_code = (
        status.HTTP_200_OK if all(checks.values()) else status.HTTP_503_SERVICE_UNAVAILABLE
    )
    return {"status": "ready" if all(checks.values()) else "degraded", "checks": checks}
