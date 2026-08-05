"""One rule: a box the public can reach does not talk to an anonymous Redis.

This lives outside `app/config.py` on purpose — it is a statement about a URL
this application *connects to*, not about a field it parses — but it is the
same move `Settings` already makes for `TRUSTED_PROXY_SECRET`: refuse to
construct the process rather than warn, because the warning is read after the
damage.

What the damage is. `docker-compose.yml` publishes Redis on the host's
loopback so `backend/tests/conftest.py` can reach it, and that host is shared
with someone else's production. Loopback is not an access control there: every
local account reaches it, which is the entire argument `app/limits.py` and
`app/config.py`'s proxy-secret validator are built on. Without AUTH an
unprivileged neighbour gets `KEYS lexloom:*` — live organisation ids out of
`lexloom:ingest:{org_id}`, and an organisation id is the RLS credential every
policy in migrations 0002-0004 keys on — plus the ability to push a
`lexloom.ingest_source` message naming any organisation it just learned,
bypassing `ensure_ingest_admitted` entirely, plus `FLUSHALL` over every
rate-limit counter.

`DEV_REDIS_PASSWORD` is refused as well as an absent one, and that is the
whole reason this module has a constant in it: the value is committed in
`docker-compose.yml`, so it is public, so on a public box it is not a
credential. `Settings._trusted_proxy_secret_is_a_secret` refuses the nginx
snippet's placeholder for exactly this reason; a default that is *usable* is
worse than one that is not, because it works.
"""

from __future__ import annotations

from urllib.parse import unquote, urlsplit

from app.config import PUBLIC_ENVS, Settings

# The default `docker-compose.yml` starts Redis with and points every service
# at. Held here rather than only there so `test_redis_auth.py` can hold the two
# to each other — a compose file that stopped passing `--requirepass` while the
# URLs kept carrying a password would fail to connect loudly, but a compose file
# that changed the default on one side only would authenticate against nothing
# and look fine.
DEV_REDIS_PASSWORD = "lexloom-local-redis"  # noqa: S105 (it is a password, published on purpose)


class UnauthenticatedRedisError(RuntimeError):
    """The configured Redis is reachable without a credential."""


def redis_password(url: str) -> str:
    """The password in a `redis://` URL, or `""` when there is none.

    `urlsplit` rather than a regex: the value is percent-encoded in a URL and
    a hand-rolled split gets `@` inside a password wrong in the direction that
    reports a password where there is none. `unquote` after it because that is
    what the client does — `redis.asyncio.from_url` percent-decodes before it
    sends AUTH, so a comparison made against the still-encoded form would let
    the development password through under a `%`-escape.
    """
    password = urlsplit(url).password
    return unquote(password) if password else ""


def assert_redis_requires_a_password(settings: Settings) -> None:
    """Called at start-up by every process that holds a Redis connection.

    Not keyed on `production` alone, for the reason `config.py` writes out at
    length for `LIMITS_ENABLED`: the public box runs `ENV=demo`, because it
    runs the development identity provider that refuses to start under
    `production`, so a check keyed on `production` would be dead code on the
    only deployment it was written for.
    """
    if settings.env not in PUBLIC_ENVS:
        return

    password = redis_password(settings.redis_url)
    if not password:
        raise UnauthenticatedRedisError(
            f"REDIS_URL carries no password and env={settings.env}: this Redis is "
            "published on the loopback interface of a shared host, where every local "
            "account can reach it, read organisation ids out of the progress and "
            "stream-slot keys, and queue ingest tasks for them. Set REDIS_PASSWORD in "
            ".env — deploy/README.md, Step 3."
        )
    if password == DEV_REDIS_PASSWORD:
        raise UnauthenticatedRedisError(
            f"REDIS_URL still carries the development password shipped in "
            f"docker-compose.yml and env={settings.env}: it is published in this "
            "repository and is therefore not a credential. Generate one with "
            "`openssl rand -hex 32` and set REDIS_PASSWORD in .env."
        )
