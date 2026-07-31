from typing import Annotated

import structlog
from fastapi import Depends, Request

from app.auth.verify import InvalidTokenError, Principal, verify_token
from app.config import get_settings
from app.errors import ProblemError

logger = structlog.get_logger(__name__)


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
        # The reason goes to the log, never to the caller. Returning it would
        # hand an attacker an oracle that distinguishes "unknown kid" from
        # "wrong audience" from "expired" from "no org_id" — precisely the
        # discrimination that makes probing a verifier productive.
        logger.info("token_rejected", reason=str(exc), request_path=request.url.path)
        raise ProblemError(
            code="invalid_token",
            status=401,
            title="Invalid credentials",
            detail="The supplied credentials are not valid.",
        ) from exc


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]
