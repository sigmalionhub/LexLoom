import time

import httpx
import structlog

logger = structlog.get_logger(__name__)

CACHE_TTL_SECONDS = 600
STALE_GRACE_SECONDS = 3600


class JwksUnavailableError(RuntimeError):
    """JWKS could not be fetched and no usable cached copy remains."""


class JwksCache:
    """Caches a JWKS document and fails closed when it cannot be refreshed.

    "No keys" must never silently mean "no verification" — a fetch failure
    that returns an empty key set turns every 401 into a 200, and it is the
    single most common real defect in this exact piece of code. A recent
    cached copy may be served for a bounded grace window (Clerk-style key
    rotation keeps the previous key valid for a while), but once that window
    passes the cache raises rather than pretending nothing is wrong.
    """

    def __init__(self, url: str, *, require_https: bool = False) -> None:
        if require_https and not url.startswith("https://"):
            # HTTPS only in production: a plaintext JWKS fetch is an
            # SSRF/MITM opening — an on-path attacker could serve a spoofed
            # key set and every subsequent token would verify against it.
            raise ValueError(f"JWKS URL must use HTTPS in production, got {url!r}")
        self._url = url
        self._value: dict | None = None
        self._fetched_at = 0.0

    async def get(self) -> dict:
        age = time.monotonic() - self._fetched_at
        if self._value is not None and age < CACHE_TTL_SECONDS:
            return self._value
        try:
            # No SSRF into the JWKS URL: it comes from configuration only,
            # never from a token's own `iss` claim, and redirects are refused
            # so a compromised or misconfigured origin cannot hop us
            # elsewhere.
            async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
                response = await client.get(self._url)
                response.raise_for_status()
            self._value = response.json()
            self._fetched_at = time.monotonic()
            return self._value
        except Exception:
            # Fail closed. "No keys" must never silently mean "no verification"
            # — that turns every 401 into a 200 and is the most common real bug
            # in this exact piece of code.
            if self._value is not None and age < STALE_GRACE_SECONDS:
                logger.warning("jwks_fetch_failed_serving_stale", age_seconds=int(age))
                return self._value
            raise JwksUnavailableError("JWKS unavailable and no fresh cached copy") from None
