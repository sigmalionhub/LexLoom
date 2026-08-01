from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from starlette.requests import Request

from app.config import Settings, get_settings
from app.limits import acquire_stream_slot, client_ip, rate_limit_bucket

# RFC 5737 TEST-NET-3 and RFC 3849 documentation addresses. Every test that
# writes a rate-limit key uses one, so a key this suite creates can never be
# confused with — or collide with — a real visitor's counter in the Redis this
# box shares with the running demo.
_OUTSIDE = "203.0.113.9"
_ANOTHER_OUTSIDE = "203.0.113.10"
_BEHIND_THE_PROXY = "198.51.100.4"


def _request(peer: str | None, forwarded: list[str] | None = None) -> Request:
    """A minimal ASGI scope: an immediate peer and whatever it claims to forward."""
    headers = [(b"x-forwarded-for", value.encode()) for value in (forwarded or [])]
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


def test_the_image_does_not_let_the_server_derive_the_client_address_too() -> None:
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


def test_the_ceilings_cannot_be_switched_off_in_production() -> None:
    """The suite runs with the ceilings off, so the mechanism that disables them
    is one environment variable away from a production box with no swap.

    It is refused at start-up rather than logged, because a warning about an
    absent limit is read after the box has already been taken down.
    """
    with pytest.raises(ValidationError, match="limits_enabled"):
        Settings(env="production", limits_enabled=False)

    # The same value is fine anywhere else, which is what makes the suite able
    # to run at all — so the guard is about production specifically, not about
    # the flag being unusable.
    assert Settings(env="ci", limits_enabled=False).limits_enabled is False


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
