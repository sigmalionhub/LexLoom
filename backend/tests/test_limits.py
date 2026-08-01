import asyncio
import json
import uuid
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from starlette.requests import Request

from app.api.v1.conversations import _releasing
from app.config import PROXY_SECRET_PLACEHOLDER, Settings, get_settings
from app.errors import ProblemError
from app.limits import PROXY_SECRET_HEADER, acquire_stream_slot, client_ip, rate_limit_bucket
from app.main import create_app

# RFC 5737 TEST-NET-3 and RFC 3849 documentation addresses. Every test that
# writes a rate-limit key uses one, so a key this suite creates can never be
# confused with — or collide with — a real visitor's counter in the Redis this
# box shares with the running demo.
_OUTSIDE = "203.0.113.9"
_ANOTHER_OUTSIDE = "203.0.113.10"
_BEHIND_THE_PROXY = "198.51.100.4"


def _request(
    peer: str | None, forwarded: list[str] | None = None, secret: str | None = None
) -> Request:
    """A minimal ASGI scope: an immediate peer and whatever it claims to forward."""
    headers = [(b"x-forwarded-for", value.encode()) for value in (forwarded or [])]
    if secret is not None:
        headers.append((PROXY_SECRET_HEADER.encode(), secret.encode()))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/demo-login",
            "query_string": b"",
            "scheme": "http",
            "server": ("test", 80),
            "headers": headers,
            "client": (peer, 42000) if peer else None,
        }
    )


def test_a_forwarding_header_from_an_untrusted_peer_is_ignored() -> None:
    """The whole limit rests on this: a caller who reaches the port directly
    cannot choose the address it is metered against."""
    assert client_ip(_request(_OUTSIDE, ["1.2.3.4"])) == _OUTSIDE


def test_a_forwarding_header_from_a_trusted_proxy_is_honoured() -> None:
    assert client_ip(_request("127.0.0.1", [_BEHIND_THE_PROXY])) == _BEHIND_THE_PROXY


def test_the_last_forwarded_entry_is_the_one_kept() -> None:
    """Reading the *first* entry is the conventional reading and the bypass.

    An appending proxy leaves the client in control of every entry but the
    last, so a limiter that reads the first is keyed on a value the caller
    picked. This deployment's nginx overwrites rather than appends, so there is
    normally one entry — but the rule has to be the safe one either way.
    """
    assert client_ip(_request("127.0.0.1", ["1.2.3.4, " + _BEHIND_THE_PROXY])) == _BEHIND_THE_PROXY


def test_two_forwarding_headers_are_read_as_one_chain_in_arrival_order() -> None:
    """A caller can send its own header and the proxy can add a second, which
    arrives as two header lines rather than one comma-joined value. Reading only
    the first line would read exactly the attacker's."""
    forwarded = ["1.2.3.4", _BEHIND_THE_PROXY]
    assert client_ip(_request("127.0.0.1", forwarded)) == _BEHIND_THE_PROXY


def test_a_malformed_forwarded_entry_falls_back_to_the_peer() -> None:
    """Anything that is not an address is not a bucket key: it would let a
    trusted-proxy path be steered into unbounded key cardinality."""
    assert client_ip(_request("127.0.0.1", ["not-an-address"])) == "127.0.0.1"


def test_a_request_with_no_peer_at_all_shares_one_bucket() -> None:
    """Some transports leave `client` unset. Sharing a single bucket is the
    fail-closed answer: it can throttle unfairly, it cannot be evaded."""
    assert client_ip(_request(None)) == "unknown"


def test_an_ipv6_bucket_is_a_network_rather_than_a_single_address() -> None:
    """A visitor with a /64 owns 2**64 addresses. Metering each one separately
    is not a limit at all, so IPv6 is bucketed by /64."""
    assert rate_limit_bucket("2001:db8:1::1") == rate_limit_bucket("2001:db8:1::dead:beef")
    assert rate_limit_bucket("2001:db8:1::1") != rate_limit_bucket("2001:db8:2::1")
    assert rate_limit_bucket(_OUTSIDE) == _OUTSIDE


def test_an_ipv4_client_on_a_dual_stack_listener_is_not_bucketed_with_the_world() -> None:
    """`::ffff:203.0.113.9` is an IPv4 client wearing an IPv6 shape, which is
    what a dual-stack listener reports.

    Masked to /64 the entire IPv4-mapped range collapses to `::/64`, so every
    IPv4 visitor would have shared one bucket — 30 logins an hour for the whole
    internet. Over-restriction rather than a bypass, and just as broken.
    """
    mapped, another = f"::ffff:{_OUTSIDE}", f"::ffff:{_ANOTHER_OUTSIDE}"
    assert rate_limit_bucket(mapped) == _OUTSIDE
    assert rate_limit_bucket(mapped) != rate_limit_bucket(another)


def test_a_trusted_peer_without_the_shared_secret_is_metered_on_itself(settings_override) -> None:
    """Position is not provenance.

    The container's peer is the bridge gateway for everything that reaches the
    published port, so on a shared host an SSRF in a neighbouring application
    arrives looking exactly like nginx. The secret is what nginx has and the
    neighbour does not.
    """
    secret = "s" * 32
    settings_override(trusted_proxies="127.0.0.1", trusted_proxy_secret=secret)

    honoured = _request("127.0.0.1", [_BEHIND_THE_PROXY], secret=secret)
    assert client_ip(honoured) == _BEHIND_THE_PROXY

    missing = _request("127.0.0.1", [_BEHIND_THE_PROXY])
    assert client_ip(missing) == "127.0.0.1"

    wrong = _request("127.0.0.1", [_BEHIND_THE_PROXY], secret="s" * 31 + "x")
    assert client_ip(wrong) == "127.0.0.1"


def _events_from(captured: str) -> dict[str, dict]:
    """The structured log lines a block of stdout carries, by event name."""
    found: dict[str, dict] = {}
    for line in captured.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and "event" in record:
            found[record["event"]] = record
    return found


def test_the_process_says_at_start_up_that_the_ceilings_are_off(
    settings_override, capsys
) -> None:
    """A process started outside Compose with LIMITS_ENABLED=false in its
    environment runs unmetered. Without this line the cheapest way to notice is
    to spend thirty logins and watch none of them be refused.

    A warning, not info: it has to be findable in a log someone is skimming.
    """
    settings_override(limits_enabled="false")
    create_app()

    logged = _events_from(capsys.readouterr().out)
    assert "limits_disabled" in logged, "an unmetered process started silently"
    assert logged["limits_disabled"]["level"] == "warning"
    assert "limits_enabled" not in logged


def test_the_process_reports_the_ceilings_it_is_actually_running(
    settings_override, capsys
) -> None:
    """Reporting only that they are on would leave the two failures that look
    identical from outside — a mis-set number, and a proxy secret that does not
    match nginx's — indistinguishable without spending the budget."""
    settings_override(limits_enabled="true", demo_login_limit=7, trusted_proxy_secret="s" * 32)
    create_app()

    logged = _events_from(capsys.readouterr().out)
    assert "limits_enabled" in logged
    assert logged["limits_enabled"]["demo_login_limit"] == 7
    assert logged["limits_enabled"]["proxy_secret_required"] is True
    # The secret itself must never reach a log line.
    assert "s" * 32 not in capsys.readouterr().out


async def test_a_live_stream_keeps_its_slot_past_the_sweep_horizon(limits_on) -> None:
    """The sweep collects slots whose process died holding them. It must not
    collect one whose stream is still running.

    The original TTL argument was wrong: it reasoned that nginx's
    `proxy_read_timeout 300s` caps a stream below the 600 s TTL.
    `proxy_read_timeout` is an *inactivity* timeout and `ping=15` writes every
    fifteen seconds, so nginx holds such a connection indefinitely and nothing
    caps a stream's length. With `FakeProvider`'s 1.3 s answer that is
    invisible; behind a slower provider — the entire point of the
    `LLMProvider` interface — the ceiling would stop being a ceiling for
    exactly the streams that cost the most.

    Driven through the endpoint's own wrapper rather than a reimplementation of
    it, because the claim is that a *streaming response* holds its slot.
    """
    limits_on(max_concurrent_streams=1, max_concurrent_streams_per_org=1,
              stream_slot_ttl_seconds=2)
    org_id = str(uuid.uuid4())

    async def slow_answer():
        for _ in range(6):
            await asyncio.sleep(0.5)
            yield {"event": "token", "data": "{}"}

    slot = await acquire_stream_slot(org_id)
    seen = 0
    async for _ in _releasing(slot, slow_answer()):
        seen += 1
        if seen == 5:
            # t is now ~2.5 s, past the 2 s horizon. Un-refreshed, this slot's
            # score is its acquisition time and the sweep would drop it, so a
            # second stream would be admitted over a ceiling of one.
            with pytest.raises(ProblemError) as refused:
                await acquire_stream_slot(org_id)
            assert refused.value.status == 503

    assert seen == 6
    # And it is genuinely given back at the end, so the refresh did not turn
    # into a leak.
    released = await acquire_stream_slot(org_id)
    await released.release()


def test_the_dockerfile_command_disables_uvicorns_own_proxy_header_handling() -> None:
    """`client_ip` is only the authority if nothing upstream has already
    rewritten the peer.

    uvicorn ships `--proxy-headers` **on**, and for a peer inside
    `--forwarded-allow-ips` (default `127.0.0.1`) its ProxyHeadersMiddleware
    replaces `scope["client"]` from X-Forwarded-For before the application
    runs. That is a second trust list, configured nowhere in this repository,
    whose fallback branch returns the *first* entry of the header — the entry a
    caller controls under an appending proxy.

    Measured, not assumed: with the flag absent, a scratch server on this box
    honoured a spoofed X-Forwarded-For from an untrusted peer and handed three
    rotating values three separate budgets. With it, all three landed in one.
    The flag is therefore part of the limit, and this test is what keeps a
    tidy-up of the CMD line from quietly removing it.

    Scope, stated because the name used to promise more: this reads the
    **Dockerfile**, not a built image. It cannot see an override from
    `docker-compose.yml`, a `docker run` that supplies its own command, or a
    developer running bare `uvicorn`. It catches the edit it names.
    """
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    # Line continuations joined and comments dropped, so the assertion is about
    # the instruction rather than about how it happens to be wrapped today.
    instructions = " ".join(
        line for line in dockerfile.replace("\\\n", " ").splitlines() if not line.startswith("#")
    )
    command = instructions[instructions.index("CMD") :]
    assert "--no-proxy-headers" in command, (
        "the image starts uvicorn with its own proxy-header handling enabled, so "
        "app.limits.client_ip is metering an address uvicorn chose by uvicorn's rules"
    )


@pytest.mark.parametrize("env", ["demo", "production"])
def test_the_ceilings_cannot_be_switched_off_where_the_public_can_reach_it(env: str) -> None:
    """The suite runs with the ceilings off, so the mechanism that disables them
    is one environment variable away from a public box with no swap.

    It is refused at start-up rather than logged, because a warning about an
    absent limit is read after the box has already been taken down.

    **`demo` is the case that matters and the one this originally missed.** The
    guard used to key on `production`, which the demo box can never be — it
    runs the dev IdP, and `DevIdp` refuses to start under `ENV=production`. So
    the check could not fire on the only deployment it was written for, while
    three documents cited it as the protection.
    """
    with pytest.raises(ValidationError, match="limits_enabled"):
        Settings(env=env, limits_enabled=False, trusted_proxy_secret="s" * 32)

    # Fine anywhere the public cannot reach, which is what lets the suite run
    # at all — the guard is about exposure, not about the flag being unusable.
    assert Settings(env="ci", limits_enabled=False).limits_enabled is False


def test_a_public_deployment_cannot_trust_a_proxy_on_position_alone() -> None:
    """The peer address only says a connection came through the published port,
    which every process on a shared box can do."""
    # `limits_enabled` is passed explicitly throughout: the suite exports
    # LIMITS_ENABLED=false, so without it every `env="demo"` construction here
    # would stop at the other validator instead of reaching this one.
    with pytest.raises(ValidationError, match="trusted_proxy_secret"):
        Settings(env="demo", limits_enabled=True, trusted_proxies="172.16.0.0/12")

    # Nothing to protect when the header is never read.
    permitted = Settings(env="demo", limits_enabled=True, trusted_proxies="none")
    assert permitted.trusted_proxy_networks == ()


def test_the_shipped_placeholder_is_not_accepted_as_the_shared_secret() -> None:
    """It is committed to this repository, so it is public by construction —
    and copying the nginx snippet without editing it is the likely mistake."""
    with pytest.raises(ValidationError, match="placeholder"):
        Settings(trusted_proxy_secret=PROXY_SECRET_PLACEHOLDER)
    with pytest.raises(ValidationError, match="at least"):
        Settings(trusted_proxy_secret="short")  # noqa: S106 (that is the point)


def test_a_demo_deployment_refuses_a_plaintext_jwks_url(settings_override) -> None:
    """`require_https` was `env == "production"`, so it was unreachable on the
    box it protects — the same dead-guard shape as the limiter flag.

    Asserted through `create_app` rather than through `JwksCache` directly,
    because what was wrong was the wiring, not the cache.
    """
    settings_override(
        env="demo",
        limits_enabled="true",
        trusted_proxy_secret="s" * 32,
        auth_jwks_url="http://localhost:8100/.well-known/jwks.json",
    )
    with pytest.raises(ValueError, match="HTTPS"):
        create_app()

    settings_override(auth_jwks_url="https://lexloom.example.test/.well-known/jwks.json")
    assert create_app() is not None


@pytest.mark.parametrize("spec", ["", "   ", ",,", "0.0.0.0/0", "127.0.0.1,::/0"])
def test_a_trusted_proxy_set_that_means_nothing_or_everything_is_refused(spec: str) -> None:
    """Both failures are silent and opposite: empty meters the whole site on
    one bucket, and a zero-length prefix lets every caller name its own key."""
    with pytest.raises(ValidationError, match="trusted_proxies"):
        Settings(trusted_proxies=spec)


def test_no_trusted_proxies_can_be_said_deliberately() -> None:
    """Rejecting the empty string has to leave a way to mean it."""
    assert Settings(trusted_proxies="none").trusted_proxy_networks == ()
    assert client_ip(_request(_OUTSIDE, ["1.2.3.4"])) == _OUTSIDE


def test_the_suite_runs_with_the_ceilings_deliberately_off() -> None:
    """Named, not incidental. Every other test in this repository creates
    organisations from 127.0.0.1 and would exhaust a per-address budget; the
    tests below turn the ceilings back on one at a time and prove they bite."""
    assert get_settings().limits_enabled is False


async def test_demo_login_is_refused_once_the_address_has_spent_its_budget(
    limits_on, app, created_orgs
) -> None:
    limits_on(demo_login_limit=2)

    async with _client_from(app, _OUTSIDE) as visitor:
        for _ in range(2):
            allowed = await visitor.post("/api/v1/auth/demo-login", json={"label": "Acme"})
            assert allowed.status_code == 200
            created_orgs.append(allowed.json()["org_id"])

        refused = await visitor.post("/api/v1/auth/demo-login", json={"label": "Acme"})

    assert refused.status_code == 429
    assert refused.headers["content-type"].startswith("application/problem+json")
    body = refused.json()
    assert body["code"] == "rate_limited"
    assert body["status"] == 429
    assert body["instance"] == "/api/v1/auth/demo-login"
    assert body["request_id"]
    # Retry-After is what turns a refusal into an instruction. It cannot exceed
    # the window, and a zero would invite an immediate retry that is refused
    # again.
    retry_after = int(refused.headers["retry-after"])
    assert 0 < retry_after <= get_settings().demo_login_window_seconds


async def test_two_addresses_do_not_share_a_budget(limits_on, app, created_orgs) -> None:
    """Otherwise the first visitor of the hour spends everyone else's."""
    limits_on(demo_login_limit=1)

    async with _client_from(app, _OUTSIDE) as first:
        spent = await first.post("/api/v1/auth/demo-login", json={"label": "Acme"})
        created_orgs.append(spent.json()["org_id"])
        assert (
            await first.post("/api/v1/auth/demo-login", json={"label": "Acme"})
        ).status_code == 429

    async with _client_from(app, _ANOTHER_OUTSIDE) as second:
        response = await second.post("/api/v1/auth/demo-login", json={"label": "Globex"})

    assert response.status_code == 200
    created_orgs.append(response.json()["org_id"])


async def test_a_stream_is_refused_when_the_organisation_holds_its_ceiling(
    limits_on, client, demo_auth, demo_org_id
) -> None:
    """One tenant must not be able to hold every connection the box has."""
    limits_on(max_concurrent_streams_per_org=1)

    conversation_id = await _conversation(client, demo_auth)
    message_id = await _ask(client, demo_auth, conversation_id, "q")
    ticket = await _ticket(client, demo_auth, message_id)

    held = await acquire_stream_slot(demo_org_id)
    try:
        refused = await client.get(_stream_url(conversation_id, message_id, ticket))
    finally:
        await held.release()

    assert refused.status_code == 503
    assert refused.headers["content-type"].startswith("application/problem+json")
    assert refused.json()["code"] == "capacity_unavailable"
    assert int(refused.headers["retry-after"]) > 0


async def test_the_global_ceiling_is_shared_by_every_organisation(
    limits_on, client, created_orgs
) -> None:
    """The per-organisation ceiling alone bounds one tenant, not the box: a
    hundred throwaway tenants each streaming once are a hundred connections."""
    limits_on(max_concurrent_streams=1, max_concurrent_streams_per_org=4)

    first_headers, first_org = await _demo_login(client, created_orgs)
    second_headers, _ = await _demo_login(client, created_orgs)

    conversation_id = await _conversation(client, second_headers)
    message_id = await _ask(client, second_headers, conversation_id, "q")
    ticket = await _ticket(client, second_headers, message_id)

    # The slot belongs to the *other* organisation, which has not touched the
    # second one's own ceiling. Only the global counter can refuse this.
    held = await acquire_stream_slot(first_org)
    try:
        refused = await client.get(_stream_url(conversation_id, message_id, ticket))
    finally:
        await held.release()

    assert refused.status_code == 503
    assert refused.json()["code"] == "capacity_unavailable"


async def test_the_slot_is_given_back_when_the_stream_finishes(
    limits_on, client, demo_auth
) -> None:
    """A ceiling that is never given back is a ceiling that closes after N
    answers and stays closed. Two answers under a ceiling of one is the whole
    claim."""
    limits_on(max_concurrent_streams=1, max_concurrent_streams_per_org=1)

    conversation_id = await _conversation(client, demo_auth)
    for _ in range(2):
        message_id = await _ask(client, demo_auth, conversation_id, "q")
        ticket = await _ticket(client, demo_auth, message_id)
        async with client.stream(
            "GET", _stream_url(conversation_id, message_id, ticket)
        ) as response:
            assert response.status_code == 200
            events = [
                line.removeprefix("event: ").strip()
                async for line in response.aiter_lines()
                if line.startswith("event: ")
            ]
        assert events[-1] == "message_end"


def _client_from(app, peer: str) -> AsyncClient:
    """A client whose ASGI scope carries `peer` as the immediate connection."""
    return AsyncClient(
        transport=ASGITransport(app=app, client=(peer, 42000)), base_url="http://test"
    )


async def _demo_login(client, created_orgs: list[str]) -> tuple[dict[str, str], str]:
    body = (await client.post("/api/v1/auth/demo-login", json={"label": "Globex"})).json()
    created_orgs.append(body["org_id"])
    return {"Authorization": f"Bearer {body['access_token']}"}, body["org_id"]


async def _conversation(client, headers) -> str:
    response = await client.post("/api/v1/conversations", json={"title": "Demo"}, headers=headers)
    return response.json()["id"]


async def _ask(client, headers, conversation_id: str, content: str) -> str:
    response = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": content},
        headers=headers,
    )
    return response.json()["message_id"]


async def _ticket(client, headers, message_id: str) -> str:
    response = await client.post(
        "/api/v1/auth/stream-tickets", json={"message_id": message_id}, headers=headers
    )
    return response.json()["ticket"]


def _stream_url(conversation_id: str, message_id: str, ticket: str) -> str:
    return f"/api/v1/conversations/{conversation_id}/messages/{message_id}/stream?ticket={ticket}"
