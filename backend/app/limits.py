"""Admission control for a demo that anybody on the internet can call.

Two ceilings live here.

`POST /api/v1/auth/demo-login` is unauthenticated and provisions a whole tenant
every time: an organisation row plus the tenant's own copy of the corpus — the
30 seeded excerpts and the 978 chunks of the preloaded GDPR — so **1009 rows
and 1.95 MiB of Postgres heap** per call. Both numbers are derived where the
limit is configured; see `demo_login_limit` in config.py, which names the file
each half comes from. Nothing about the endpoint bounds how often that happens,
and the deployment target is a shared box with 2.2 GB free and 2 GB of swap,
where memory pressure is felt by every application on the host and the OOM
killer at the end of it may pick a *neighbouring* one. So tenant creation is
metered per client address.

**That per-call cost was got wrong here, twice over.** This docstring, and the
two comments below that did arithmetic on it, said 31 rows and 11.7 kB. That
was true while a tenant got the seed corpus alone and its chunks carried no
embedding, and it was not re-derived when the seed gained vectors or when
preloading gave every tenant the whole GDPR. Nothing about the ceilings
themselves was wrong; the argument for their size was.

Every SSE stream holds a connection open for as long as it generates, so the
second ceiling is on concurrency: how many streams may be open at once, in
total and per organisation.

Both live in Redis rather than in process memory. Not for durability — a
counter that survives a restart is not the point — but because the ceiling has
to mean the same thing to every process that serves a request. An in-process
counter is a per-worker ceiling wearing a global name.

Failure is refusal, not admission. If Redis cannot be reached the ceilings
cannot be enforced, and proceeding unmetered is exactly the outcome this module
exists to prevent; a Redis that is down already fails `GET /health/ready`, so
the operator is being told about the real problem elsewhere.
"""

import hmac
import ipaddress
import time
import uuid
from dataclasses import dataclass, field

import redis.asyncio as aioredis
import structlog
from fastapi import Request
from redis.exceptions import RedisError

from app.config import Settings, get_settings
from app.errors import ProblemError

logger = structlog.get_logger(__name__)

# Every key this module writes starts here. The deployment shares one Redis
# database with Celery, and the development box shares it with a running copy
# of the demo, so a prefix is what lets this module's state be swept without
# reaching a queue.
KEY_PREFIX = "lexloom:limits:"

_DEMO_LOGIN_KEY = KEY_PREFIX + "demo-login:{bucket}:{window}"
_GLOBAL_STREAMS_KEY = KEY_PREFIX + "streams:global"
_ORG_STREAMS_KEY = KEY_PREFIX + "streams:org:{org_id}"

# The header nginx uses to prove a request came *through* it rather than
# merely from its direction. See `TRUSTED_PROXY_SECRET` in config.py.
PROXY_SECRET_HEADER = "x-lexloom-proxy"  # noqa: S105 (a header name, not a credential)

# IPv6 is metered by /64, not by address: per-address counting would give a
# single visitor 2**64 budgets, which is not a limit.
#
# /64 is the smallest unit an operator hands out whole, and the honest limit of
# this choice is that it is *only* the smallest. A residential customer
# delegated a /56 holds 256 of these buckets and a business with a /48 holds
# 65,536, so an IPv6 visitor's effective budget scales with the size of their
# delegation. Bucketing at /48 instead would fix that and start merging
# unrelated end sites into one bucket, which fails in the direction that denies
# service to strangers. /64 fails in the direction that is merely generous.
_IPV6_BUCKET_BITS = 64

# A demo answer is 41 tokens at 30 ms — about 1.3 s, and 44 frames once the
# three non-token events are counted — so a slot that is genuinely in use frees
# within seconds. Five is long enough that a client honouring it is not
# hammering, short enough that a visitor who waits does not conclude the page
# is broken.
_STREAM_RETRY_AFTER_SECONDS = 5
# An unreachable Redis is an operator-scale problem, not a queueing one, and
# telling a client to come back in five seconds would be a lie.
_BACKEND_RETRY_AFTER_SECONDS = 30

# Redis is on localhost or on the Compose network, so a slow connect means
# something is wrong rather than far away. Without these the client's default
# is no timeout at all, and one unhealthy Redis would hold every request that
# touches it open until the proxy gave up.
_REDIS_TIMEOUT_SECONDS = 2


def _redis(settings: Settings) -> aioredis.Redis:
    """A client for one operation, closed by the caller's `async with`.

    Deliberately not a module-level singleton. A connection pool belongs to the
    event loop it was created on, and this suite gives every test its own loop —
    the same reason `conftest` disposes the SQLAlchemy engine between tests.
    Redis is local, the connect is sub-millisecond, and the operations here run
    either alongside 1009 row inserts or at the two ends of a stream, so the
    cost does not show. `app/api/v1/health.py` opens its client the same way.
    """
    return aioredis.from_url(
        settings.redis_url,
        socket_connect_timeout=_REDIS_TIMEOUT_SECONDS,
        socket_timeout=_REDIS_TIMEOUT_SECONDS,
    )


def client_ip(request: Request) -> str:
    """The address this request is metered against.

    Two conditions gate the forwarding header, and both have to hold.

    *Position.* The immediate peer must be in `TRUSTED_PROXIES`. On the demo
    box nginx terminates TLS and the peer is nginx, so the real address only
    exists in `X-Forwarded-For`; anywhere else the header is whatever the
    caller typed, and honouring it would let anyone pick their own limit key —
    a limit worse than none, because it looks like protection.

    *Provenance.* When `TRUSTED_PROXY_SECRET` is set, the peer must also
    present it in `X-LexLoom-Proxy`. Position alone is not enough on a shared
    host: the container's peer is the Compose bridge gateway for every
    connection through the published port, so a server-side request forgery in
    a neighbouring application would arrive looking exactly like nginx. The
    secret is set by `deploy/nginx/snippets/lexloom-proxy.conf` and by nothing
    else on the box.

    When the peer is trusted, the **last** entry is taken, never the first.
    There is exactly one proxy in front of this application and it is
    `deploy/nginx/snippets/lexloom-proxy.conf`, which sets the header to
    `$remote_addr` — overwriting rather than appending — so the header arrives
    carrying one value: the address nginx accepted the connection from. Reading
    the last entry is correct under that vhost and stays correct under the
    conventional appending form, where the caller controls every entry except
    the last. Reading the first would be the bypass in the appending case.

    What this does *not* implement: a chain of several proxies. With two
    appending proxies in front, the last entry is the inner proxy's address
    rather than the client's, and both this function and that vhost would have
    to be reconsidered together.

    This is only the authority if nothing upstream has already rewritten the
    peer, which uvicorn does by default — see the `--no-proxy-headers` comment
    in `backend/Dockerfile` and
    test_the_dockerfile_command_disables_uvicorns_own_proxy_header_handling.
    """
    peer = request.client.host if request.client else ""
    if not peer:
        # No peer to key on. One shared bucket is unfair but not evadable,
        # which is the right way round for a ceiling.
        return "unknown"
    if not _is_trusted_proxy(peer):
        return peer
    if not _carries_the_proxy_secret(request):
        # Right network, wrong provenance. On a shared box the peer address
        # only says the connection came through the published port, which
        # every process on the host can do.
        logger.warning("forwarded_for_without_proxy_secret", peer=peer)
        return peer

    # `getlist`, not `get`: a caller can send its own header and a proxy can add
    # a second, which arrives as two header lines. `get` returns only the first
    # — which in that case is precisely the caller's.
    forwarded = ",".join(request.headers.getlist("x-forwarded-for"))
    for entry in reversed(forwarded.split(",")):
        entry = entry.strip()
        if not entry:
            continue
        try:
            return str(ipaddress.ip_address(entry))
        except ValueError:
            # Not an address, so not a key: an arbitrary string here would be
            # unbounded key cardinality in Redis, chosen by whoever can reach a
            # trusted proxy. Fall back to the peer, which is always real.
            logger.warning("forwarded_for_unparsable", entry=entry, peer=peer)
            return peer
    return peer


def rate_limit_bucket(address: str) -> str:
    """The key an address is counted under: itself for IPv4, its /64 for IPv6."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        # `client_ip` only returns this for a request with no peer at all.
        return address
    # An IPv4-mapped address (`::ffff:203.0.113.9`) is an IPv4 client wearing an
    # IPv6 shape — which every dual-stack listener produces. Masked to /64 the
    # whole mapped range collapses to `::/64`, so *every* IPv4 client reaching
    # such a listener would share one bucket: not a bypass, but 30 logins an
    # hour for the internet. Unwrap first, then meter as what it is.
    mapped = parsed.ipv4_mapped if parsed.version == 6 else None
    if mapped is not None:
        return str(mapped)
    if parsed.version == 6:
        return str(ipaddress.ip_network(f"{parsed}/{_IPV6_BUCKET_BITS}", strict=False))
    return str(parsed)


def _carries_the_proxy_secret(request: Request) -> bool:
    """Does this request prove it came *through* the proxy, not just from it?

    An unset secret means peer position is the whole test, which is the right
    answer for local development and for a box this application has to itself.
    `Settings` makes it mandatory on a public deployment that trusts any proxy.

    `compare_digest` rather than `==`: the comparison is against a secret and
    the caller controls one side of it.
    """
    secret = get_settings().trusted_proxy_secret
    if not secret:
        return True
    return hmac.compare_digest(request.headers.get(PROXY_SECRET_HEADER, ""), secret)


def _is_trusted_proxy(peer: str) -> bool:
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        # A unix-socket peer arrives as a path. It cannot be matched against a
        # network, and treating it as trusted would trust a header on a
        # transport nothing in this deployment uses.
        return False
    return any(address in network for network in get_settings().trusted_proxy_networks)


def _rate_limited(retry_after: int) -> ProblemError:
    return ProblemError(
        code="rate_limited",
        status=429,
        title="Too many requests",
        # No numbers and no count: the limit and how much of it is spent are
        # both information about the ceiling, and Retry-After already says the
        # only thing a client needs in order to behave.
        detail="Too many demo organisations from this address. Retry after Retry-After seconds.",
        headers={"Retry-After": str(retry_after)},
    )


def _capacity_unavailable(retry_after: int) -> ProblemError:
    return ProblemError(
        code="capacity_unavailable",
        status=503,
        title="Capacity unavailable",
        # One detail for every trigger. Which ceiling was reached, and whether a
        # ceiling was reached at all or merely could not be consulted, goes to
        # the log with the request_id — the same trade `invalid_token` makes.
        detail="The service cannot start this work right now. Retry after Retry-After seconds.",
        headers={"Retry-After": str(retry_after)},
    )


async def enforce_demo_login_rate_limit(request: Request) -> None:
    """Meter tenant creation per client address. A FastAPI dependency.

    A fixed window, counted with INCR against a key that names the window, not
    a sliding one. Two reasons, and the first is the honest cost: at a window
    boundary an address can spend one window's budget and the next window's
    back to back, so the real worst case is twice the configured limit in a
    short burst — at the default, 60 tenants, ~61k rows and ~117 MiB of
    Postgres written in minutes rather than spread over an hour.

    That is bounded, and it is what the second reason is paid with: the
    alternative, a sorted set of timestamps per address, costs roughly ten
    times the memory in a Redis capped at 48 MB with `allkeys-lru`, where the
    eviction that buys is the *silent loss of the counter itself*. A doubled
    burst the purge reclaims within a day is a better-shaped failure than a
    limiter that quietly stops counting.
    """
    settings = get_settings()
    if not settings.limits_enabled:
        return

    address = client_ip(request)
    window = settings.demo_login_window_seconds
    now = int(time.time())
    # The window index is part of the key, so expiry does the resetting and
    # nothing has to be reset on a read path.
    window_ends_at = (now // window + 1) * window
    key = _DEMO_LOGIN_KEY.format(bucket=rate_limit_bucket(address), window=now // window)

    try:
        async with _redis(settings) as redis:
            async with redis.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                # EXPIREAT the window's own end rather than EXPIRE(window): the
                # remaining TTL is then exactly what Retry-After should say, and
                # a busy address cannot keep pushing its key's expiry forward.
                pipe.expireat(key, window_ends_at)
                spent, _ = await pipe.execute()
    except (RedisError, OSError):
        logger.warning("limits_backend_unavailable", check="demo_login", exc_info=True)
        raise _capacity_unavailable(_BACKEND_RETRY_AFTER_SECONDS) from None

    if spent > settings.demo_login_limit:
        logger.info(
            "demo_login_rate_limited", address=address, spent=spent, limit=settings.demo_login_limit
        )
        raise _rate_limited(max(window_ends_at - now, 1))


@dataclass
class StreamSlot:
    """One admitted stream's reservation, released when the stream ends."""

    org_id: str
    token: str
    refreshed_at: float = field(default_factory=time.time)

    async def touch(self) -> None:
        """Say the stream is still alive, so the sweep does not collect it.

        The sweep exists for slots whose process died holding them, and it can
        only tell the difference if a living stream keeps saying so.

        **The previous version of this had no `touch` and justified it wrongly.**
        It argued that nginx's `proxy_read_timeout 300s` caps a stream, so no
        live stream could reach the 600 s TTL. `proxy_read_timeout` is an
        *inactivity* timeout between successive reads, and
        `EventSourceResponse(ping=15)` writes every fifteen seconds — so nginx
        holds such a connection indefinitely and a stream can outlive any fixed
        TTL. Demonstrated: a live stream's slot was swept and a second stream
        was admitted over the ceiling. Harmless with `FakeProvider`'s 1.3 s
        answer, and real the moment a slower provider is swapped in behind
        `LLMProvider` — which is what that interface is for. The ceiling would
        have stopped being a ceiling for exactly the streams that cost most.

        Throttled to a quarter of the TTL, so a 44-token answer never touches
        Redis at all and a long one touches it a handful of times.

        `ZADD ... XX` updates an existing member and never creates one. Without
        it a touch racing a release would resurrect a slot that nothing will
        ever give back — and a slot that has already been swept stays swept
        rather than reappearing with a fresh score.
        """
        if not self.token:
            return
        settings = get_settings()
        now = time.time()
        if now - self.refreshed_at < max(settings.stream_slot_ttl_seconds / 4, 1):
            return
        self.refreshed_at = now
        org_key = _ORG_STREAMS_KEY.format(org_id=self.org_id)
        try:
            async with _redis(settings) as redis:
                async with redis.pipeline(transaction=True) as pipe:
                    for key in (_GLOBAL_STREAMS_KEY, org_key):
                        pipe.zadd(key, {self.token: now}, xx=True)
                        pipe.expire(key, settings.stream_slot_ttl_seconds)
                    await pipe.execute()
        except (RedisError, OSError):
            # Logged, not raised: the caller is mid-answer. The cost of a
            # missed touch is that this slot ages towards the sweep, and the
            # next touch fixes it.
            logger.warning("stream_slot_not_refreshed", org_id=self.org_id, exc_info=True)

    async def release(self) -> None:
        """Give the slot back. Idempotent, and never raises into a stream.

        Idempotent because it is called from two places on purpose — see
        `app/api/v1/conversations.py` — and ZREM of an absent member is a
        no-op. Silent on failure because by the time this runs the caller
        either has its whole answer or has walked away, and turning a Redis
        blip into an exception on the response path would take away the one
        thing that worked. The leak it leaves behind is swept by the TTL prune
        in `acquire_stream_slot`.
        """
        if not self.token:
            return
        settings = get_settings()
        try:
            async with _redis(settings) as redis:
                async with redis.pipeline(transaction=True) as pipe:
                    pipe.zrem(_GLOBAL_STREAMS_KEY, self.token)
                    pipe.zrem(_ORG_STREAMS_KEY.format(org_id=self.org_id), self.token)
                    await pipe.execute()
        except (RedisError, OSError):
            logger.warning("stream_slot_not_released", org_id=self.org_id, exc_info=True)


async def acquire_stream_slot(org_id: str) -> StreamSlot:
    """Reserve one of the concurrent-stream slots, or refuse with a 503.

    Each ceiling is a sorted set of live slot tokens, scored by the second the
    slot was last known to be alive. A plain counter cannot be used: it is only
    ever correct if every increment is matched by a decrement, and the process
    holding a stream can be killed. Scores make the set self-healing — every
    acquisition first drops entries older than `stream_slot_ttl_seconds`.

    The score is a *liveness* timestamp, not the start time, because nothing
    caps how long a stream may run — see `StreamSlot.touch`. A living stream
    refreshes its own score, so what the sweep collects is a slot that has gone
    `stream_slot_ttl_seconds` without a sign of life, which for a process that
    died holding one is always.

    Admission is add-then-count rather than count-then-add. Two requests
    racing on count-then-add can both read `limit - 1` and both proceed; with
    add-then-count each sees its own membership, so the worst case is that both
    stand down when one could have gone — over-refusing under contention, which
    is the direction a ceiling should fail in.
    """
    settings = get_settings()
    if not settings.limits_enabled:
        # An empty token makes `release` a no-op, so the caller needs no branch.
        return StreamSlot(org_id=org_id, token="")

    token = uuid.uuid4().hex
    now = time.time()
    stale_before = now - settings.stream_slot_ttl_seconds
    global_key = _GLOBAL_STREAMS_KEY
    org_key = _ORG_STREAMS_KEY.format(org_id=org_id)

    try:
        async with _redis(settings) as redis:
            async with redis.pipeline(transaction=True) as pipe:
                for key in (global_key, org_key):
                    pipe.zremrangebyscore(key, "-inf", stale_before)
                    pipe.zadd(key, {token: now})
                    pipe.zcard(key)
                    # Bounds the key itself, not the slots: the prune above is
                    # what expires slots. This only keeps an organisation that
                    # never streams again from leaving a set behind for ever.
                    # `touch` re-arms it, so a long stream cannot have the key
                    # expire out from under its own membership.
                    pipe.expire(key, settings.stream_slot_ttl_seconds)
                results = await pipe.execute()

            global_open, org_open = results[2], results[6]
            refusal = None
            if org_open > settings.max_concurrent_streams_per_org:
                refusal = "org_ceiling"
            elif global_open > settings.max_concurrent_streams:
                refusal = "global_ceiling"

            if refusal:
                async with redis.pipeline(transaction=True) as pipe:
                    pipe.zrem(global_key, token)
                    pipe.zrem(org_key, token)
                    await pipe.execute()
                logger.info(
                    "stream_admission_refused",
                    reason=refusal,
                    org_id=org_id,
                    org_open=org_open,
                    global_open=global_open,
                )
                raise _capacity_unavailable(_STREAM_RETRY_AFTER_SECONDS)
    except (RedisError, OSError):
        logger.warning("limits_backend_unavailable", check="stream_admission", exc_info=True)
        raise _capacity_unavailable(_BACKEND_RETRY_AFTER_SECONDS) from None

    return StreamSlot(org_id=org_id, token=token, refreshed_at=now)


def report_configuration(settings: Settings) -> None:
    """Say once, at start-up, whether the ceilings are on and what they are.

    A process started outside Compose with `LIMITS_ENABLED=false` in its
    environment runs unmetered and, without this, says nothing about it — the
    cheapest way to notice would be to spend thirty logins and watch none of
    them be refused. An absent limit that announces itself is an operational
    problem; an absent limit that is silent is an incident.

    `warning` when they are off, deliberately: it is the line someone has to be
    able to find in a log they are skimming.
    """
    if not settings.limits_enabled:
        logger.warning(
            "limits_disabled",
            env=settings.env,
            detail="demo-login is unmetered and concurrent streams are uncapped",
        )
        return
    logger.info(
        "limits_enabled",
        env=settings.env,
        demo_login_limit=settings.demo_login_limit,
        demo_login_window_seconds=settings.demo_login_window_seconds,
        max_concurrent_streams=settings.max_concurrent_streams,
        max_concurrent_streams_per_org=settings.max_concurrent_streams_per_org,
        trusted_proxies=settings.trusted_proxies,
        # The value never goes near a log line; whether one is required does.
        # Without it a mismatched secret looks exactly like a working limiter
        # that meters every visitor on one bucket.
        proxy_secret_required=bool(settings.trusted_proxy_secret),
    )
