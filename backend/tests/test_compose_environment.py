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
from app.worker.celery_app import _parse_purge_schedule_seconds, celery_app
from app.worker.tasks import purge_expired_orgs

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
ENV_EXAMPLE = REPO_ROOT / ".env.example"

# The service that serves HTTP, and the only one an operator tunes. The worker
# is deliberately given less — see the `TRUSTED_PROXIES` comment beside it.
HTTP_SERVICE = "api"

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


def _build_context(service: dict) -> object | None:
    """`build: ./backend` and `build: {context: ./backend, args: ...}` are the
    same image. Comparing the raw value to a string meant that the first
    service to need a build argument dropped out of this set — and the
    parametrised test above would have gone on passing while covering one
    service fewer.
    """
    build = service.get("build")
    return build.get("context") if isinstance(build, dict) else build


def _application_services() -> dict[str, dict[str, str]]:
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    services = {
        name: service.get("environment") or {}
        for name, service in compose["services"].items()
        if _build_context(service) == APPLICATION_BUILD_CONTEXT
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


# `NAME=value` and the commented `# NAME=value` form `.env.example` uses for
# every optional setting. Both are the file presenting a knob to an operator.
_ENV_EXAMPLE_ASSIGNMENT = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]*)\s*=", re.MULTILINE)


def _settings_documented_in_env_example() -> set[str]:
    named = set(_ENV_EXAMPLE_ASSIGNMENT.findall(ENV_EXAMPLE.read_text(encoding="utf-8")))
    # Compose-level variables (the database passwords) are interpolated into
    # other values rather than read by the application, so they are not
    # `Settings` fields and are not in scope here.
    return {name for name in named if name.lower() in Settings.model_fields}


def test_every_setting_env_example_documents_is_forwarded_by_compose():
    """A knob `.env.example` presents must reach the container that reads it.

    `.env` sits at the repository root and the build context is `./backend`, so
    the file is not in the image and `Settings(env_file=".env")` finds nothing
    inside the container. Compose reads it, interpolates it into the values in
    the service's `environment:` block, and that block is the entire channel.
    A setting `.env.example` documents and that block omits is a knob that
    turns nothing, with no symptom: the operator sets it, restarts, and the
    default keeps applying.

    That shipped. `docker-compose.yml` had a long comment explaining the trap
    for `ERROR_TYPE_BASE_URL` and forwarded none of the five admission-control
    numbers underneath it — so an operator lowering `MAX_CONCURRENT_STREAMS` on
    a box that was running out of memory would have changed nothing.

    Discovered from the two files rather than listed, so a setting added to
    either one tomorrow is covered without editing this test.
    """
    documented = _settings_documented_in_env_example()
    assert documented, ".env.example named no Settings field — the parser is broken"

    forwarded = set(_application_services()[HTTP_SERVICE])
    missing = sorted(documented - forwarded)
    assert not missing, (
        f".env.example documents {missing} but the `{HTTP_SERVICE}` service in "
        f"{COMPOSE_FILE.name} does not forward them. Setting one in .env would reach "
        "Compose and stop there, and the container would keep using the default."
    )


# --- The `beat` service actually schedules the purge ------------------------
#
# `test_every_application_service_starts_on_the_public_box[beat]` above proves
# `beat` can construct `Settings` and start — the same thing the compose-smoke
# job in ci.yml proves by watching `docker compose ps`. Neither one proves
# anything about what `beat` schedules: a typo in
# `PURGE_EXPIRED_ORGS_SCHEDULE_SECONDS`, a broken task-name string inside
# `beat_schedule`, or the schedule-reading code losing its wiring would all
# leave the container running and every HTTP check green, with nothing ever
# scheduled. `test_demo_orgs.py` already covers the purge SQL itself
# (`test_purge_removes_expired_orgs_only` and neighbours) by calling the task
# directly; what is untested there is the scheduling mechanism between
# `docker-compose.yml`'s environment block and a firing task, which is what
# the tests below are for.

def test_purge_schedule_default_is_hourly():
    assert _parse_purge_schedule_seconds("3600") == 3600.0


def test_purge_schedule_honours_a_small_valid_override():
    assert _parse_purge_schedule_seconds("5") == 5.0


@pytest.mark.parametrize("raw", ["0", "0.0", "-1", "-0.5", "-3600"])
def test_purge_schedule_rejects_zero_and_negative(raw):
    """The regression the reviewer measured directly: `celery.schedules.schedule`
    treats a non-positive `run_every` as always due, which would fire
    `purge_expired_orgs` in a tight loop against a Postgres shared with other
    applications on this box, rather than crash the container the way a
    non-numeric value already does.
    """
    with pytest.raises(ValueError):
        _parse_purge_schedule_seconds(raw)


@pytest.mark.parametrize("raw", ["not-a-number", "", "nan", "inf", "-inf"])
def test_purge_schedule_rejects_non_numeric_and_non_finite(raw):
    with pytest.raises(ValueError):
        _parse_purge_schedule_seconds(raw)


def test_beat_schedule_names_the_task_that_is_actually_registered():
    """Catches a typo in the task-name string inside `beat_schedule`.

    `beat` schedules a task name freely — it does not check that any worker
    owns it, so a typo here does not crash `beat` either; it schedules a task
    that fires into the void and is discovered only when someone notices the
    row count growing. Compared against `purge_expired_orgs.name`, the
    decorated task's own registered name, rather than a second copy of the
    string literal, so the two cannot drift apart silently.
    """
    entry = celery_app.conf.beat_schedule["purge-expired-demo-orgs"]
    assert entry["task"] == purge_expired_orgs.name
    assert purge_expired_orgs.name in celery_app.tasks

def test_the_two_embed_batch_constants_are_the_same_number():
    """`650m` is sized on a batch of 32, and two separate constants spell it.

    `app/ingest/jobs.py` and `app/worker/tasks.py` each define their own
    `EMBED_BATCH = 32`, deliberately — `jobs.py` says so in the comment above
    its copy: the two tasks are allowed to diverge on this number in future
    without one edit silently changing the other's memory budget.

    That reasoning is about *divergence being permitted*. It is not a reason
    for divergence to be **silent**, and today it would be: both constants are
    used by processes that run inside the worker container, whose `mem_limit`
    and `memswap_limit` in `docker-compose.yml` are 650m — a figure derived
    from the measured curve beside both constants (253 MiB at 16, 315 MiB at
    32, 444 MiB at 64). Raising one of the two to 64 moves a ratified memory
    ceiling. Nothing in the test suite or in Compose would go red; the failure
    would arrive as an OOM-killed worker on the deploy box, which is the one
    place this project has said it cannot afford one (2.2 GB of RAM, no swap,
    shared with a live deployment).

    So this asserts equality — not to forbid the divergence `jobs.py` reserves,
    but to make claiming it an explicit act. **If this test goes red, the
    correct response is not to change the other constant to match.** It is to
    re-derive `650m` against the larger of the two and edit
    `docker-compose.yml`'s `worker` limits and this test together, then delete
    the equality claim in favour of whatever the new relationship is.

    `app/evals/corpus.py` imports the `worker.tasks` copy rather than defining
    a third, so it moves with that one and needs nothing here.
    """
    from app.ingest.jobs import EMBED_BATCH as INGEST_EMBED_BATCH
    from app.worker.tasks import EMBED_BATCH as WORKER_EMBED_BATCH

    assert INGEST_EMBED_BATCH == WORKER_EMBED_BATCH, (
        f"app/ingest/jobs.py says {INGEST_EMBED_BATCH}, app/worker/tasks.py says "
        f"{WORKER_EMBED_BATCH}. Both run in the worker container, whose 650m limit "
        "was measured at a batch of 32. Re-derive the limit before letting these "
        "differ, and change docker-compose.yml with them."
    )

    worker = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))["services"]["worker"]
    assert worker["mem_limit"] == "650m", (
        f"the worker's mem_limit is {worker['mem_limit']!r}, not '650m' — the "
        "docstring above and the EMBED_BATCH comments both reason from 650m, so "
        "one of them is now stale."
    )
    assert worker["memswap_limit"] == worker["mem_limit"], (
        "memswap_limit must equal mem_limit, or the container can take its limit "
        "a second time in swap (docker-compose.yml:25)."
    )
