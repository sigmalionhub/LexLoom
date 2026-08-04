"""`GET /api/v1/status` — public, unauthenticated, and deliberately thin.

Everything this endpoint returns comes straight from `app.observability`,
which is where the reasoning about what is measured and what is published
lives. This module only wires the snapshot to a route: `response_model` is
`ServiceStatus` so the payload's shape reaches `openapi.json` and, from there,
`frontend/lib/api-types.ts` — see `lib/api.ts:20-32` on why an endpoint
without a response model teaches the generated client nothing.

Unauthenticated on purpose: it takes no token, sees no organisation and reads
no org-scoped table, so it carries none of the residual-purge argument
migration 0004 makes for a credential — see the comment beside its entry in
`EXPECTED_ROUTES` (`backend/tests/test_demo_orgs.py`).
"""

from fastapi import APIRouter

from app.observability import ServiceStatus, snapshot

router = APIRouter(prefix="/api/v1", tags=["status"])


@router.get("/status", response_model=ServiceStatus)
async def get_status() -> ServiceStatus:
    return snapshot()
