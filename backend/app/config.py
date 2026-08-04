import ipaddress
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

# Environments the public internet can reach. Everything that must not be
# optional on a public box keys on this set rather than on `production`
# specifically.
#
# `demo` exists because this deployment is neither of the two names the code
# had. It is a public, internet-facing box that *deliberately* runs the
# development identity provider, so it cannot be `production` — `DevIdp`
# refuses to start there, correctly. Calling it `local` was the alternative,
# and that is what made every `env == "production"` guard in this codebase
# decorative on the one box they were written for.
PUBLIC_ENVS = frozenset({"demo", "production"})

# What `TRUSTED_PROXIES` must say to mean "no proxy is trusted, never read the
# forwarding header". Spelled out rather than expressed as an empty string,
# because an empty string is also what a broken interpolation produces — and
# the two mean opposite things to an operator while looking identical here.
NO_TRUSTED_PROXIES = "none"

# The value shipped in deploy/nginx/snippets/lexloom-proxy.conf. Rejected
# outright: a shared secret that is public in a git repository is not a secret,
# and copying the snippet without editing it is the likely mistake.
PROXY_SECRET_PLACEHOLDER = "REPLACE_ME_WITH_TRUSTED_PROXY_SECRET"  # noqa: S105 (the opposite)

_MIN_PROXY_SECRET_LENGTH = 16


@lru_cache(maxsize=4)
def parse_trusted_proxies(spec: str) -> tuple[IpNetwork, ...]:
    """Parse a comma-separated list of addresses and CIDRs.

    Cached on the string because it is read on every request that is metered,
    and the string changes at most once per process.

    A bare address parses as a single-host network (`strict=False` accepts
    `127.0.0.1` as `127.0.0.1/32`), so an operator never has to write a mask to
    name one proxy.

    Two values are refused rather than obeyed, because both are far more likely
    to be accidents than intentions and both fail silently in opposite
    directions:

    *Empty.* The forwarding header would never be read, so behind nginx every
    visitor on earth would share the proxy's own bucket — one budget for the
    whole site. Write `none` to mean it deliberately.

    *A zero-length prefix* (`0.0.0.0/0`, `::/0`). Every peer becomes a trusted
    proxy, so anyone who can reach the port names their own limit key and the
    limit is decorative. Name the range you actually trust.
    """
    if spec.strip().lower() == NO_TRUSTED_PROXIES:
        return ()
    entries = [entry.strip() for entry in spec.split(",") if entry.strip()]
    if not entries:
        raise ValueError(
            "trusted_proxies is empty, which would silently meter every visitor on the "
            f"proxy's own address; write {NO_TRUSTED_PROXIES!r} if that is deliberate"
        )
    networks = tuple(ipaddress.ip_network(entry, strict=False) for entry in entries)
    for network in networks:
        if network.prefixlen == 0:
            raise ValueError(
                f"trusted_proxies contains {network}, which trusts every peer and lets "
                "anyone who reaches this port choose the address they are metered on"
            )
    return networks


class Settings(BaseSettings):
    """Application configuration, validated at import time.

    A missing variable must stop the process at startup rather than surface
    as a 500 on a user's first request.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: Literal["local", "ci", "demo", "production"] = "local"
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

    # Google AI Studio key for the Gemini generator option (app/chat/gemini.py).
    # Empty is a working state, not a misconfiguration: with no key,
    # `build_provider(GeneratorChoice.GEMINI)` refuses rather than silently
    # falling back to the fake, and the demo runs on `FakeProvider` alone
    # (Task 30 decides when to offer the choice at all). Sent as the
    # `x-goog-api-key` header, never a query parameter — see app/chat/gemini.py.
    gemini_api_key: str = Field(
        default="",
        description="Google AI Studio API key for the Gemini generator option; empty disables it",
    )

    # --- Admission control (app/limits.py) ---------------------------------
    #
    # The numbers are sized for the deployment target in deploy/README.md: a
    # shared box with 2 vCPU, 2.2 GB free and 2 GB of swap, where memory
    # pressure slows every application on the host and the OOM kill at the end
    # of it can take a neighbouring one rather than this.

    limits_enabled: bool = Field(
        default=True,
        description="Master switch for the demo-login rate limit and the stream ceilings",
    )

    trusted_proxies: str = Field(
        default="127.0.0.1,::1",
        description=(
            "Addresses and CIDRs whose X-Forwarded-For header is believed, or "
            "'none'. Loopback only by default; a containerised deployment has "
            "to name the bridge gateway it is actually reached from."
        ),
    )

    # Being on the right network is necessary and, on a shared box, not
    # sufficient: the container's peer is the Compose bridge gateway, so
    # *anything* that reaches the published port arrives as a trusted proxy —
    # including an SSRF in a neighbouring application. This secret is the
    # second half: nginx sets it, so a caller has to have come through nginx
    # and not merely from nginx's direction.
    #
    # Empty means peer-only, which is right for a box this application has to
    # itself and for local development. It is mandatory in a PUBLIC_ENVS
    # deployment that trusts any proxy at all — see the validator below.
    trusted_proxy_secret: str = Field(
        default="",
        description=(
            "Shared secret nginx sends in X-LexLoom-Proxy; when set, "
            "X-Forwarded-For is read only from a peer that also presents it"
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
    # How long a slot may go without a sign of life before the sweep collects
    # it. It is NOT a cap on how long a stream may run: nothing caps that.
    # `proxy_read_timeout 300s` is an inactivity timeout between successive
    # reads and `EventSourceResponse(ping=15)` writes every fifteen seconds, so
    # nginx holds a live stream indefinitely. A running stream refreshes its
    # own slot (`StreamSlot.touch`), which is what makes this a crash backstop
    # rather than a ceiling on stream length. 600s = four ping intervals of
    # slack over the 150s refresh period.
    stream_slot_ttl_seconds: int = Field(default=600, ge=1)

    @field_validator("trusted_proxies")
    @classmethod
    def _trusted_proxies_parse(cls, value: str) -> str:
        """A typo here silently widens or narrows who can set their own limit
        key, so it is rejected at start-up rather than at the first request."""
        parse_trusted_proxies(value)
        return value

    @field_validator("trusted_proxy_secret")
    @classmethod
    def _trusted_proxy_secret_is_a_secret(cls, value: str) -> str:
        if not value:
            return value
        if value == PROXY_SECRET_PLACEHOLDER:
            raise ValueError(
                "trusted_proxy_secret is still the placeholder shipped in "
                "deploy/nginx/snippets/lexloom-proxy.conf, which is public in this "
                "repository and therefore not a secret"
            )
        if len(value) < _MIN_PROXY_SECRET_LENGTH:
            raise ValueError(
                f"trusted_proxy_secret must be at least {_MIN_PROXY_SECRET_LENGTH} "
                "characters; generate one with `openssl rand -hex 32`"
            )
        return value

    @model_validator(mode="after")
    def _limits_stay_on_where_the_public_can_reach_it(self) -> "Settings":
        """`LIMITS_ENABLED=false` is how the test suite runs; refuse it here.

        Without this the flag is one environment variable between a public,
        unauthenticated, 31-rows-per-call endpoint and a shared box. A warning
        would not do: it is read after the OOM killer has already chosen a
        victim, and the victim may be somebody else's application.

        Keyed on `PUBLIC_ENVS`, not on `production` alone. Keyed on
        `production` this validator was **dead code on the only box it was
        written for**: that deployment runs the dev IdP, so `ENV=production`
        makes `create_app` raise, so `ENV` there is whatever else the runbook
        says — and three documents nonetheless cited this check as the
        protection. What was actually keeping the flag out of the container was
        that Compose did not forward the variable: accidental, undocumented,
        and one natural-looking `LIMITS_ENABLED: ${LIMITS_ENABLED:-true}` line
        away from being gone.
        """
        if self.env in PUBLIC_ENVS and not self.limits_enabled:
            raise ValueError(
                f"limits_enabled=False is refused when env={self.env}: the demo-login "
                "rate limit and the stream ceilings are what keep this deployment from "
                "taking the box down"
            )
        return self

    @model_validator(mode="after")
    def _a_public_box_does_not_trust_a_proxy_on_position_alone(self) -> "Settings":
        """On a shared host, "came from the right network" is not enough.

        The API port is published on loopback, so the peer is the container
        bridge gateway for *every* connection through it — nginx's and any
        other process's on the box alike. Without the shared secret, a
        server-side request forgery in a neighbouring application forges
        `X-Forwarded-For` freely and the rate limit becomes decorative. Host
        hygiene is not enforcement, so this is required rather than advised.

        Not required when nothing is trusted: with `TRUSTED_PROXIES=none` the
        forwarding header is never read and there is nothing for a secret to
        protect.
        """
        if (
            self.env in PUBLIC_ENVS
            and self.trusted_proxy_networks
            and not self.trusted_proxy_secret
        ):
            raise ValueError(
                f"trusted_proxy_secret is required when env={self.env} and "
                "trusted_proxies is not 'none': without it anything that can reach the "
                "API port — including an SSRF in a neighbouring application on this "
                "shared box — can choose the address it is metered on"
            )
        return self

    @property
    def trusted_proxy_networks(self) -> tuple[IpNetwork, ...]:
        return parse_trusted_proxies(self.trusted_proxies)


@lru_cache
def get_settings() -> Settings:
    return Settings()
