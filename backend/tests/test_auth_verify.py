import time

import jwt
import pytest

from app.auth.dev_idp import DevIdp
from app.auth.verify import InvalidTokenError, verify_token

ISSUER, AUDIENCE = "https://test-idp", "lexloom-api"


@pytest.fixture
def idp() -> DevIdp:
    return DevIdp(issuer=ISSUER, audience=AUDIENCE)


def test_valid_token_yields_a_principal(idp: DevIdp) -> None:
    principal = verify_token(idp.mint("user_1", "org_acme"), idp.jwks(), ISSUER, AUDIENCE)
    assert principal.subject == "user_1"
    assert principal.org_id == "org_acme"


def test_expired_token_is_rejected(idp: DevIdp) -> None:
    with pytest.raises(InvalidTokenError, match="expired"):
        verify_token(idp.mint("u", "o", ttl=-10), idp.jwks(), ISSUER, AUDIENCE)


def test_wrong_audience_is_rejected(idp: DevIdp) -> None:
    token = idp.mint("u", "o", audience="some-other-api")
    with pytest.raises(InvalidTokenError, match="audience"):
        verify_token(token, idp.jwks(), ISSUER, AUDIENCE)


def test_token_signed_by_another_issuer_is_rejected(idp: DevIdp) -> None:
    other = DevIdp(issuer=ISSUER, audience=AUDIENCE)
    with pytest.raises(InvalidTokenError):
        verify_token(other.mint("u", "o"), idp.jwks(), ISSUER, AUDIENCE)


@pytest.mark.filterwarnings("ignore::jwt.warnings.InsecureKeyLengthWarning")
def test_unknown_kid_is_rejected_rather_than_trying_every_key(idp: DevIdp) -> None:
    # The forged token below deliberately signs with a short, weak HMAC
    # secret to prove the verifier rejects it on kid mismatch alone --
    # PyJWT's own key-strength warning about that secret is expected noise
    # from the attack payload, not a signal about our verifier.
    token = idp.mint("u", "o")
    tampered = jwt.encode(
        jwt.decode(token, options={"verify_signature": False}, audience=AUDIENCE),
        "secret",
        algorithm="HS256",
        headers={"kid": "nope"},
    )
    with pytest.raises(InvalidTokenError, match="key"):
        verify_token(tampered, idp.jwks(), ISSUER, AUDIENCE)


def test_hs256_token_signed_with_the_public_key_is_rejected(idp: DevIdp) -> None:
    """Algorithm confusion: the verifier must never read `alg` from the token."""
    key = idp.jwks()["keys"][0]
    forged = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": "attacker", "org_id": "org_victim",
         "exp": int(time.time()) + 60},
        key["n"],
        algorithm="HS256",
        headers={"kid": key["kid"]},
    )
    with pytest.raises(InvalidTokenError):
        verify_token(forged, idp.jwks(), ISSUER, AUDIENCE)


def test_token_without_org_id_is_rejected(idp: DevIdp) -> None:
    token = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": "u", "exp": int(time.time()) + 60},
        idp._private, algorithm="RS256", headers={"kid": idp.jwks()["keys"][0]["kid"]},
    )
    with pytest.raises(InvalidTokenError, match="org_id"):
        verify_token(token, idp.jwks(), ISSUER, AUDIENCE)
