from typing import Annotated

from fastapi import Depends, Request

from app.auth.verify import InvalidTokenError, Principal, verify_token
from app.config import get_settings
from app.errors import ProblemError


async def current_principal(request: Request) -> Principal:
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        raise ProblemError(
            code="missing_credentials", status=401,
            title="Authentication required", detail="Expected a Bearer token",
        )
    settings = get_settings()
    jwks = await request.app.state.jwks_cache.get()
    try:
        return verify_token(header[7:], jwks, settings.auth_issuer, settings.auth_audience)
    except InvalidTokenError as exc:
        raise ProblemError(
            code="invalid_token", status=401,
            title="Invalid credentials", detail=str(exc),
        ) from exc


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]
