"""The Redis this stack publishes on a shared host must require a password.

Two halves, and both are needed. The Compose half proves the server is started
with `--requirepass` and that every client URL carries the same variable — a
server that demands AUTH and clients that do not send it fails loudly, but a
default changed on one side only would authenticate against nothing and look
fine. The `Settings` half proves a public deployment cannot start while the
committed development password is still in use, which is the failure the
runbook is one forgotten line away from.

`deploy/README.md` used to present "127.0.0.1 only" as the security property.
It is not one here: the deployment host is shared, every local account reaches
host loopback, and that is the whole argument `TRUSTED_PROXY_SECRET` exists to
make (`app/config.py`). These tests are what keeps the corrected claim true.
"""

import re
from pathlib import Path

import pytest
import yaml
from conftest import LIMITS_TEST_REDIS_URL

from app.config import Settings
from app.redis_auth import (
    DEV_REDIS_PASSWORD,
    UnauthenticatedRedisError,
    assert_redis_requires_a_password,
    redis_password,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
ENV_EXAMPLE = REPO_ROOT / ".env.example"

# The exact interpolation every side must use: one variable, one default. Held
# as one string rather than as a variable name plus a default checked
# separately, because the two drifting apart is the specific failure this file
# exists to catch.
EXPECTED_INTERPOLATION = f"${{REDIS_PASSWORD:-{DEV_REDIS_PASSWORD}}}"


def _compose() -> dict:
    return yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))


def _settings(**overrides) -> Settings:
    """A `Settings` the public box could actually hold.

    `trusted_proxies="none"` so the proxy-secret validator does not fire first
    and mask what this file is testing; `_env_file=None` because the repository
    root may hold a real `.env` that the container would not have;
    `limits_enabled=True` passed explicitly because conftest exports
    `LIMITS_ENABLED=false` for the whole suite and `env=demo` refuses it — an
    ambient value would fail this file for a reason it is not about.
    """
    values = {
        "env": "demo",
        "limits_enabled": True,
        "database_url": "postgresql+asyncpg://lexloom_app:x@postgres:5432/lexloom",
        "redis_url": "redis://:s3cret@redis:6379/0",
        "auth_jwks_url": "https://lexloom.example/.well-known/jwks.json",
        "trusted_proxies": "none",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


# --- What Compose actually starts -------------------------------------------


def test_the_redis_server_is_started_with_requirepass():
    command = " ".join(_compose()["services"]["redis"]["command"].split())
    assert f"--requirepass {EXPECTED_INTERPOLATION}" in command, (
        "docker-compose.yml starts Redis without --requirepass, or with a different "
        f"default than app/redis_auth.py's DEV_REDIS_PASSWORD. Command: {command!r}"
    )


@pytest.mark.parametrize("service", ["api", "worker", "beat"])
def test_every_service_authenticates_against_that_same_password(service):
    """A client that omits the password cannot connect at all, so this is not
    about the connection working — it is about the *variable* being the same
    one. A service left on a hardcoded password would keep working until the
    operator set REDIS_PASSWORD, and would then be the only thing on the box
    that could not reach the broker."""
    url = _compose()["services"][service]["environment"]["REDIS_URL"]
    assert url == f"redis://:{EXPECTED_INTERPOLATION}@redis:6379/0", (
        f"the {service} service's REDIS_URL does not carry ${{REDIS_PASSWORD}}: {url!r}"
    )


def test_env_example_documents_the_password_with_the_same_default():
    """`.env.example` is where the operator meets this setting, and Step 3 of
    deploy/README.md tells them to replace it. A different default here would
    silently give a developer a Redis they cannot reach."""
    assignment = re.search(
        r"^REDIS_PASSWORD=(.*)$", ENV_EXAMPLE.read_text(encoding="utf-8"), re.MULTILINE
    )
    assert assignment, ".env.example does not document REDIS_PASSWORD"
    assert assignment.group(1).strip() == DEV_REDIS_PASSWORD


def test_the_suite_reaches_redis_with_a_password_too():
    """The suite talks to the Compose instance from the host, so the moment
    that instance requires AUTH the suite has to send it. Checked on the
    derived limits URL rather than on `REDIS_URL` itself, because that
    derivation (host, port *and credentials* carried over, only the database
    number replaced) is the part that could silently drop the password."""
    assert redis_password(LIMITS_TEST_REDIS_URL), (
        "conftest's Redis URL for database 1 carries no password — the derivation "
        "dropped it, and every limits test would fail against an authenticated server"
    )


# --- What a public deployment refuses to start with -------------------------


def test_a_public_env_refuses_an_anonymous_redis():
    with pytest.raises(UnauthenticatedRedisError) as refused:
        assert_redis_requires_a_password(_settings(redis_url="redis://redis:6379/0"))
    assert "REDIS_PASSWORD" in str(refused.value)


def test_a_public_env_refuses_the_password_this_repository_publishes():
    """The one that would actually happen: the operator copies `.env.example`,
    changes the three database passwords and the proxy secret, and never
    notices this line. It authenticates, so nothing fails — which is why it has
    to be refused by name."""
    with pytest.raises(UnauthenticatedRedisError):
        assert_redis_requires_a_password(
            _settings(redis_url=f"redis://:{DEV_REDIS_PASSWORD}@redis:6379/0")
        )


def test_a_public_env_accepts_a_password_of_its_own():
    assert_redis_requires_a_password(_settings())  # does not raise


@pytest.mark.parametrize("env", ["local", "ci"])
def test_development_is_left_alone(env):
    """`pytest` and a laptop's `docker compose up` both run against the
    committed default, and must keep doing so with no setup step."""
    assert_redis_requires_a_password(
        _settings(env=env, redis_url=f"redis://:{DEV_REDIS_PASSWORD}@localhost:6379/0")
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("redis://redis:6379/0", ""),
        ("redis://:pw@redis:6379/0", "pw"),
        ("redis://user:pw@redis:6379/0", "pw"),
        # A password containing an `@` is why this is not a regex.
        ("redis://:p%40w@redis:6379/0", "p@w"),
    ],
)
def test_redis_password_reads_the_url_the_way_the_client_does(url, expected):
    assert redis_password(url) == expected
