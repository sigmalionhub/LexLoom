"""Every containerised process must be able to construct `Settings` on the
public box.

A variable set in `.env` reaches a container only if its service names it.
`.env` sits at the repository root while the build context is `./backend`, so it
is not in the image and the container's own `Settings(env_file=".env")` finds
nothing — Compose reads the file, interpolates it into the values written here,
and nothing else crosses. That asymmetry is invisible from either side: the
operator sees a variable set, the service sees a default.

It shipped once. The worker was given three variables, `ENV` among them, and
`app/worker/celery_app.py` calls `get_settings()` at module import — so with
`ENV=demo` the container raised a `ValidationError` for a setting that only the
HTTP process has any use for, exited, and stayed exited. The API next to it was
fine, `curl /health/ready` answered, and the deployment had no background worker
at all.

So this reads the compose file rather than a list: a service added tomorrow is
covered without editing anything here, and a new `PUBLIC_ENVS`-keyed validator
that a non-HTTP process cannot satisfy fails here instead of on the box.
"""

import re
from pathlib import Path

import pytest
import yaml

from app.config import Settings

COMPOSE_FILE = Path(__file__).resolve().parents[2] / "docker-compose.yml"

# The image every application process runs. Postgres and Redis are upstream
# images that never construct `Settings`, so they are not in scope.
APPLICATION_BUILD_CONTEXT = "./backend"

# What deploy/README.md tells the operator to put in `.env` for the public box,
# and nothing more. Values are shaped like the real ones — the proxy secret has
# to clear a minimum length and is refused if it is still the placeholder from
# the nginx snippet, and the JWKS URL has to be HTTPS under a public env.
DEMO_DOT_ENV = {
    "ENV": "demo",
    "MIGRATOR_DB_PASSWORD": "0f1e2d3c4b5a69788796a5b4c3d2e1f0",
    "APP_DB_PASSWORD": "1f2e3d4c5b6a79808897a6b5c4d3e2f1",
    "WORKER_DB_PASSWORD": "2f3e4d5c6b7a89909998a7b6c5d4e3f2",
    "ERROR_TYPE_BASE_URL": "https://lexloom.example",
    "AUTH_JWKS_URL": "https://lexloom.example/.well-known/jwks.json",
    "TRUSTED_PROXY_SECRET": "3f4e5d6c7b8a9900aa09b8c7d6e5f403",
}

# Compose's `${VAR}` and `${VAR:-default}`. Only the two forms this file uses;
# a third would fail visibly here rather than being resolved wrongly.
_INTERPOLATION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _interpolate(value: object, host_env: dict[str, str]) -> str:
    def substitute(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        return host_env.get(name, default if default is not None else "")

    return _INTERPOLATION.sub(substitute, str(value))


def _application_services() -> dict[str, dict[str, str]]:
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    services = {
        name: service.get("environment") or {}
        for name, service in compose["services"].items()
        if service.get("build") == APPLICATION_BUILD_CONTEXT
    }
    assert services, (
        f"no service in {COMPOSE_FILE.name} builds {APPLICATION_BUILD_CONTEXT!r} — "
        "this test would pass by finding nothing to check"
    )
    return services


@pytest.mark.parametrize("service", sorted(_application_services()))
def test_every_application_service_starts_on_the_public_box(service, monkeypatch):
    """Construct `Settings` exactly as the container would, under `ENV=demo`.

    The ambient environment is cleared first, because the suite exports
    `LIMITS_ENABLED=false` and a developer may export anything else: what is
    under test is what Compose forwards, not what this shell happens to hold.
    """
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
    for name, value in _application_services()[service].items():
        monkeypatch.setenv(name, _interpolate(value, DEMO_DOT_ENV))

    # `_env_file=None`: the repository root may hold a real `.env`, which the
    # container would not have. Only the forwarded variables may decide this.
    settings = Settings(_env_file=None)

    assert settings.env == "demo", (
        f"the {service} service did not receive ENV — a public box would run it "
        "with every PUBLIC_ENVS guard inert"
    )
