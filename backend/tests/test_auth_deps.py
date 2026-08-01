import jwt
from httpx import ASGITransport, AsyncClient

from app.auth.deps import CurrentPrincipal
from app.logging import configure_logging
from app.main import create_app


class _StaticJwks:
    """A fake JwksCache that serves a fixed key set with no network I/O."""

    def __init__(self, jwks: dict) -> None:
        self._jwks = jwks

    async def get(self) -> dict:
        return self._jwks


async def test_rejection_reason_is_logged_but_not_returned_to_the_caller(capsys) -> None:
    """The 401 body must not become an oracle for probing the verifier.

    Same shape as test_tracebacks_do_not_leak_frame_locals: the sentinel
    value here is the specific rejection reason ("no key matches kid
    'nope'"). It must land in the log so an operator can diagnose real
    failures, but never in the response body a caller receives — otherwise a
    caller probing with crafted tokens learns exactly which check failed and
    therefore which one to attack next.
    """
    configure_logging()
    app = create_app()
    idp = app.state.dev_idp
    app.state.jwks_cache = _StaticJwks(idp.jwks())

    @app.get("/whoami")
    async def whoami(principal: CurrentPrincipal) -> dict[str, str]:
        return {"subject": principal.subject, "org_id": principal.org_id}

    # A validly-signed token (same private key) but with an unknown `kid` --
    # rejected before signature verification is even attempted.
    token = idp.mint("attacker", "org_x")
    tampered = jwt.encode(
        jwt.decode(token, options={"verify_signature": False}),
        idp._private,
        algorithm="RS256",
        headers={"kid": "nope"},
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/whoami", headers={"Authorization": f"Bearer {tampered}"})

    assert response.status_code == 401
    body = response.json()
    assert body["code"] == "invalid_token"
    assert body["detail"] == "The supplied credentials are not valid."
    assert "kid" not in body["detail"]
    assert "nope" not in body["detail"]

    log_output = capsys.readouterr().out
    assert "token_rejected" in log_output
    assert "nope" in log_output


async def test_missing_bearer_header_yields_a_fixed_generic_detail() -> None:
    app = create_app()

    @app.get("/whoami")
    async def whoami(principal: CurrentPrincipal) -> dict[str, str]:
        return {"subject": principal.subject, "org_id": principal.org_id}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/whoami")

    assert response.status_code == 401
    body = response.json()
    assert body["code"] == "missing_credentials"
    assert body["detail"] == "Expected a Bearer token"
