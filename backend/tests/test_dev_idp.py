import jwt
import pytest

from app.auth.dev_idp import DevIdp


def test_jwks_exposes_a_public_key_and_no_private_material() -> None:
    idp = DevIdp(issuer="https://test-idp", audience="lexloom-api")
    jwks = idp.jwks()
    key = jwks["keys"][0]
    assert key["kty"] == "RSA"
    assert key["use"] == "sig"
    assert key["alg"] == "RS256"
    assert key["kid"]
    assert "d" not in key and "p" not in key and "q" not in key


def test_minted_token_carries_the_expected_claims() -> None:
    idp = DevIdp(issuer="https://test-idp", audience="lexloom-api")
    token = idp.mint(subject="user_1", org_id="org_acme")
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "RS256"
    assert header["kid"] == idp.jwks()["keys"][0]["kid"]
    claims = jwt.decode(token, options={"verify_signature": False}, audience="lexloom-api")
    assert claims["sub"] == "user_1"
    assert claims["org_id"] == "org_acme"
    assert claims["iss"] == "https://test-idp"


def test_two_instances_do_not_share_keys() -> None:
    """Keys are ephemeral. Nothing is committed, and a restart invalidates tokens.

    Compare the modulus, not just the kid. `kid` is a uuid4 generated
    independently of the RSA key, so a regression that memoised one keypair
    across instances would still produce distinct kids and leave a kid-only
    assertion green — while destroying the property this test exists to protect.
    """
    first, second = DevIdp("https://i", "a"), DevIdp("https://i", "a")
    first_key, second_key = first.jwks()["keys"][0], second.jwks()["keys"][0]
    assert first_key["n"] != second_key["n"], "RSA modulus must differ per instance"
    assert first_key["kid"] != second_key["kid"]


def test_refuses_to_construct_in_production_without_override(monkeypatch) -> None:
    monkeypatch.setenv("ENV", "production")
    with pytest.raises(RuntimeError, match="dev IdP"):
        DevIdp("https://i", "a", allow_production=False)
