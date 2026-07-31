from dataclasses import dataclass

import jwt
from jwt import PyJWKSet

ALLOWED_ALGORITHMS = ["RS256"]
# PyJWT applies `leeway` to `exp` as well as to `nbf` and `iat`, so this is not
# only a tolerance for a slow clock — it is also how long an expired token keeps
# working. NTP-synchronised hosts drift by low single-digit seconds, so five
# covers real skew while keeping the window an attacker gains to a minimum.
CLOCK_SKEW_SECONDS = 5


class InvalidTokenError(Exception):
    """Token failed verification. The message is safe to log, not to return."""


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str
    org_id: str


def verify_token(token: str, jwks: dict, issuer: str, audience: str) -> Principal:
    """Verify a Clerk-shaped RS256 token against a JWKS.

    This is the half of the auth story that gets reused against real Clerk:
    only AUTH_JWKS_URL changes. Every rule below exists because breaking it is
    a known, exploited bypass.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise InvalidTokenError("malformed token header") from exc

    kid = header.get("kid")
    if not kid:
        raise InvalidTokenError("token header carries no key id")

    try:
        key_set = PyJWKSet.from_dict(jwks)
    except Exception as exc:
        # A fetched-but-malformed key set is still a failure to verify, not an
        # internal error. Letting it escape turns an upstream provider
        # misconfiguration into an opaque 500 on every request instead of a
        # clean, loggable 401.
        raise InvalidTokenError("key set is malformed") from exc

    # Select by kid. Never iterate over every key: that turns a rotation
    # window into an oracle and hides which key actually verified.
    signing_key = next((k for k in key_set.keys if k.key_id == kid), None)
    if signing_key is None:
        raise InvalidTokenError(f"no key matches kid {kid!r}")

    try:
        claims = jwt.decode(
            token,
            signing_key.key,
            # Algorithms come from configuration, never from the token header.
            # Otherwise an attacker signs HS256 using the RSA public key as the
            # HMAC secret and the verifier accepts it.
            algorithms=ALLOWED_ALGORITHMS,
            issuer=issuer,
            # PyJWT does not verify `aud` at all unless it is passed here.
            audience=audience,
            leeway=CLOCK_SKEW_SECONDS,
            # "iat" is deliberately not required: it carries no expiry
            # semantics of its own (exp already governs freshness) and Clerk
            # is not contractually guaranteed to always emit it, so requiring
            # it would reject otherwise-valid tokens on a technicality.
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise InvalidTokenError("token has expired") from exc
    except jwt.InvalidAudienceError as exc:
        raise InvalidTokenError("token audience does not match") from exc
    except jwt.PyJWTError as exc:
        raise InvalidTokenError("token signature or claims are invalid") from exc

    org_id = claims.get("org_id")
    if not org_id:
        raise InvalidTokenError("token carries no org_id claim")

    return Principal(subject=claims["sub"], org_id=org_id)
