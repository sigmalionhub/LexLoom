from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.auth.demo import DEMO_TOKEN_TTL_SECONDS, provision_demo_org

router = APIRouter(tags=["auth"])


class DemoLoginRequest(BaseModel):
    label: str = Field(max_length=64)


@router.get("/.well-known/jwks.json")
async def jwks(request: Request) -> dict:
    return request.app.state.dev_idp.jwks()


@router.post("/api/v1/auth/demo-login")
async def demo_login(payload: DemoLoginRequest, request: Request) -> dict[str, object]:
    """Mint a throwaway organisation for this visitor.

    Shared demo orgs would put one visitor's upload in front of the next, and a
    project whose headline claim is per-org isolation cannot seat every visitor
    in the same tenant. An ephemeral org makes the demo demonstrate the thesis:
    two browsers, two orgs, mutually invisible data.

    `expires_in` describes the token, so it is the same constant the token was
    minted with — not a second literal that agrees with `mint()`'s default by
    coincidence and stops agreeing the moment either is tuned.
    """
    org = await provision_demo_org(payload.label)
    token = request.app.state.dev_idp.mint(
        subject=org.external_id, org_id=org.id, ttl=DEMO_TOKEN_TTL_SECONDS
    )
    return {
        "access_token": token,
        "org_id": org.id,
        "org_name": org.name,
        "expires_in": DEMO_TOKEN_TTL_SECONDS,
    }
