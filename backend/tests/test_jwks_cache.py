"""The brief's own tests cover token shapes, not the cache.

"No keys" must never silently mean "no verification" — a JWKS fetch failure
that returns an empty key set turns every 401 into a 200, which is the most
common real defect in this exact piece of code. These tests commit that
fail-closed proof to the suite rather than leaving it as a one-off script.
"""

import time

import pytest

from app.auth.jwks import CACHE_TTL_SECONDS, STALE_GRACE_SECONDS, JwksCache, JwksUnavailableError

URL = "https://idp.example/.well-known/jwks.json"


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


class _SucceedingClient:
    """Stands in for httpx.AsyncClient so tests never touch the network."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def __aenter__(self) -> "_SucceedingClient":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(self, url: str) -> _FakeResponse:
        return _FakeResponse({"keys": [{"kid": "fresh"}]})


class _FailingClient:
    """A JWKS endpoint that is unreachable, e.g. down or filtered by SSRF egress rules."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def __aenter__(self) -> "_FailingClient":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(self, url: str) -> _FakeResponse:
        raise ConnectionError("connection refused")


def test_require_https_in_production_rejects_a_plaintext_url() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        JwksCache("http://idp.example/jwks.json", require_https=True)


def test_require_https_defaults_to_false_so_local_dev_keeps_working() -> None:
    JwksCache("http://localhost:8100/.well-known/jwks.json")  # must not raise


async def test_successful_fetch_populates_the_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.auth.jwks.httpx.AsyncClient", _SucceedingClient)
    cache = JwksCache(URL)
    assert await cache.get() == {"keys": [{"kid": "fresh"}]}


async def test_raises_when_fetch_fails_and_no_cached_copy_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The core fail-closed proof: an unreachable JWKS endpoint with a cold
    cache must raise, never return an empty (or missing) key set that would
    make every token look unverifiable-but-harmless instead of rejected.
    """
    monkeypatch.setattr("app.auth.jwks.httpx.AsyncClient", _FailingClient)
    cache = JwksCache(URL)
    with pytest.raises(JwksUnavailableError):
        await cache.get()


async def test_serves_a_stale_copy_within_the_grace_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.auth.jwks.httpx.AsyncClient", _FailingClient)
    cache = JwksCache(URL)
    cache._value = {"keys": [{"kid": "cached"}]}
    cache._fetched_at = time.monotonic() - (STALE_GRACE_SECONDS - 5)
    assert await cache.get() == {"keys": [{"kid": "cached"}]}


async def test_raises_once_the_stale_grace_window_has_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.auth.jwks.httpx.AsyncClient", _FailingClient)
    cache = JwksCache(URL)
    cache._value = {"keys": [{"kid": "cached"}]}
    cache._fetched_at = time.monotonic() - (STALE_GRACE_SECONDS + 5)
    with pytest.raises(JwksUnavailableError):
        await cache.get()


async def test_within_ttl_the_cached_copy_is_served_without_a_fetch_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.auth.jwks.httpx.AsyncClient", _FailingClient)
    cache = JwksCache(URL)
    cache._value = {"keys": [{"kid": "cached"}]}
    cache._fetched_at = time.monotonic() - (CACHE_TTL_SECONDS - 1)
    assert await cache.get() == {"keys": [{"kid": "cached"}]}
