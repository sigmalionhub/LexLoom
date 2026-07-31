from fastapi import APIRouter, Response, status

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def liveness() -> dict[str, str]:
    """Process is running. Never touches dependencies.

    Conflating this with readiness is the classic cause of a supervisor
    killing a healthy process during a transient database blip.
    """
    return {"status": "alive"}


@router.get("/health/ready")
async def readiness(response: Response) -> dict[str, object]:
    """Dependencies are reachable. Wired to real checks in Task 3 and Task 4."""
    checks: dict[str, bool] = {"database": False, "redis": False}
    response.status_code = (
        status.HTTP_200_OK if all(checks.values()) else status.HTTP_503_SERVICE_UNAVAILABLE
    )
    return {"status": "ready" if all(checks.values()) else "degraded", "checks": checks}
