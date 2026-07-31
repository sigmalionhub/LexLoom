from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.db.engine import engine

router = APIRouter(tags=["health"])


async def _database_ok() -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
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
    """Dependencies are reachable. Redis check is wired in Task 4."""
    checks: dict[str, bool] = {"database": await _database_ok(), "redis": False}
    response.status_code = (
        status.HTTP_200_OK if all(checks.values()) else status.HTTP_503_SERVICE_UNAVAILABLE
    )
    return {"status": "ready" if all(checks.values()) else "degraded", "checks": checks}
