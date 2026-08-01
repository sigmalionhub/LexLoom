import base64
import time
import uuid

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa


def _b64u(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class DevIdp:
    """A Clerk-shaped token issuer for the demo.

    The keypair is generated in memory at startup and never written anywhere.
    A committed signing key would let any visitor mint a token for any org_id,
    which would make the tenant-isolation demonstration unfalsifiable — and
    isolation is this project's entire thesis.
    """

    def __init__(
        self, issuer: str, audience: str, *, env: str, allow_production: bool = False
    ) -> None:
        """`env` is passed in, and is required.

        It used to be read here as `os.getenv("ENV")`, which is a *different*
        source of truth from the one the rest of the application uses:
        `Settings` also reads `.env`, and the environment variable does not
        exist for a value that arrives from that file. So with `ENV=production`
        set only in `.env` — which `.env.example` and the README both describe
        as the way to refuse this — `Settings.env` was `"production"` while
        `os.getenv("ENV")` was `None`, and `create_app()` started the dev IdP on
        a box documented as being behind a real identity provider. Measured, not
        reasoned about.

        Required rather than defaulted, because a default here is a guard that
        is silently absent whenever a caller forgets it, which is the shape of
        the bug being fixed.
        """
        if env == "production" and not allow_production:
            raise RuntimeError(
                "Refusing to start the dev IdP in production. Point AUTH_JWKS_URL at a real "
                "identity provider, or pass allow_production=True deliberately."
            )
        self.issuer = issuer
        self.audience = audience
        self._kid = uuid.uuid4().hex
        self._private = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def jwks(self) -> dict[str, list[dict[str, str]]]:
        numbers = self._private.public_key().public_numbers()
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "use": "sig",
                    "alg": "RS256",
                    "kid": self._kid,
                    "n": _b64u(numbers.n),
                    "e": _b64u(numbers.e),
                }
            ]
        }

    def mint(self, subject: str, org_id: str, ttl: int = 3600, audience: str | None = None) -> str:
        now = int(time.time())
        return jwt.encode(
            {
                "iss": self.issuer,
                "aud": audience or self.audience,
                "sub": subject,
                "org_id": org_id,
                "iat": now,
                "nbf": now,
                "exp": now + ttl,
            },
            self._private,
            algorithm="RS256",
            headers={"kid": self._kid},
        )
