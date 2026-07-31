from fastapi import APIRouter, Request

router = APIRouter(tags=["auth"])


@router.get("/.well-known/jwks.json")
async def jwks(request: Request) -> dict:
    return request.app.state.dev_idp.jwks()
