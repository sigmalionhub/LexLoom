import ipaddress
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


@lru_cache(maxsize=4)
def parse_trusted_proxies(spec: str) -> tuple[IpNetwork, ...]:
    """Parse a comma-separated list of addresses and CIDRs.

    Cached on the string because it is read on every request that is metered,
    and the string changes at most once per process.

    A bare address parses as a single-host network (`strict=False` accepts
    `127.0.0.1` as `127.0.0.1/32`), so an operator never has to write a mask to
    name one proxy.
    """
    return tuple(
        ipaddress.ip_network(entry.strip(), strict=False)
        for entry in spec.split(",")
        if entry.strip()
    )


class Settings(BaseSettings):
    """Application configuration, validated at import time.

    A missing variable must stop the process at startup rather than surface
    as a 500 on a user's first request.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: Literal["local", "ci", "production"] = "local"
    api_port: int = 8100

    database_url: str = Field(description="asyncpg DSN for the application role")
    redis_url: str = Field(description="Redis DSN used as Celery broker and cache")

    error_type_base_url: str = Field(
        default="http://localhost:8100",
        description="Base URL the RFC 9457 `type` field is built from",
    )

    auth_jwks_url: str = Field(
        default="http://localhost:8100/.well-known/jwks.json",
        description="JWKS endpoint; point at Clerk to use Clerk instead of the dev IdP",
    )
    auth_issuer: str = "https://lexloom.dev-idp"
    auth_audience: str = "lexloom-api"

    # --- Admission control (app/limits.py) ---------------------------------
    #
    # The numbers are sized for the deployment target in deploy/README.md: a
    # shared box with 2 vCPU, 2.2 GB free and no swap, where an OOM kill can
    # take a neighbouring application rather than this one.

    limits_enabled: bool = Field(
        default=True,
        description="Master switch for the demo-login rate limit and the stream ceilings",
    )

    trusted_proxies: str = Field(
        default="127.0.0.1,::1",
        description=(
            "Addresses and CIDRs whose X-Forwarded-For header is believed. "
            "Loopback only by default; a containerised deployment has to name "
            "the bridge gateway it is actually reached from."
        ),
    )

    # 30 tenants an hour from one address. Each demo-login writes 31 rows and
    # about 11.7 kB (an organisation plus its own copy of the 30-chunk corpus),
    # so this bounds one address at ~930 rows/hour and, across the 24-hour
    # organisation TTL, at ~22k rows and ~8 MB before the purge reclaims any of
    # it. Above the ceiling a genuine visitor can reach: one login opens the
    # demo, two demonstrate the isolation thesis in two browsers, and 30 still
    # covers a NATed office where thirty different people look in the same hour.
    demo_login_limit: int = Field(default=30, ge=1)
    demo_login_window_seconds: int = Field(default=3600, ge=1)

    # 16 concurrent streams for the whole process, against a SQLAlchemy pool of
    # 10 + 10 overflow. A stream holds no connection while it generates, but it
    # takes one at each end (the message lookup and retrieval, then the write),
    # so 16 leaves four connections for everything that is not a stream even if
    # every admitted stream reaches for one at the same instant.
    max_concurrent_streams: int = Field(default=16, ge=1)
    # Four per organisation, so one tenant cannot hold the box: a tab, its
    # EventSource reconnect and a second tab all fit, and at least four
    # different organisations can always be streaming at once.
    max_concurrent_streams_per_org: int = Field(default=4, ge=1)
    # A slot older than this is assumed to belong to a process that died
    # holding it. nginx caps a stream at proxy_read_timeout 300s
    # (deploy/nginx/snippets/lexloom-proxy.conf), so no live stream can reach
    # 600s and the sweep can only ever collect a leak.
    stream_slot_ttl_seconds: int = Field(default=600, ge=1)

    @field_validator("trusted_proxies")
    @classmethod
    def _trusted_proxies_parse(cls, value: str) -> str:
        """A typo here silently widens or narrows who can set their own limit
        key, so it is rejected at start-up rather than at the first request."""
        parse_trusted_proxies(value)
        return value

    @model_validator(mode="after")
    def _limits_stay_on_in_production(self) -> "Settings":
        """`LIMITS_ENABLED=false` is how the test suite runs; refuse it here.

        Without this the flag is one environment variable between a public,
        unauthenticated, 31-rows-per-call endpoint and a box with no swap. A
        warning would not do: it is read after the OOM killer has already
        chosen a victim, and the victim may be somebody else's application.
        """
        if self.env == "production" and not self.limits_enabled:
            raise ValueError(
                "limits_enabled=False is refused when env=production: the demo-login "
                "rate limit and the stream ceilings are what keep this deployment from "
                "taking the box down"
            )
        return self

    @property
    def trusted_proxy_networks(self) -> tuple[IpNetwork, ...]:
        return parse_trusted_proxies(self.trusted_proxies)


@lru_cache
def get_settings() -> Settings:
    return Settings()
