import os
import pathlib
import uuid
from urllib.parse import urlsplit, urlunsplit

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url

# Imported above the environment block, unlike every other `app.` import in this
# file, and safe there: `app.redis_auth` reads nothing from the environment at
# import time (`app.config` only *defines* `Settings`; it is instantiated
# lazily). Imported rather than copied because the same string has to appear in
# `docker-compose.yml`, in that module and in the default below, and a copy that
# drifted would show up as an authentication failure in whichever of the three
# was not edited. `test_redis_auth.py` holds all three together.
from app.redis_auth import DEV_REDIS_PASSWORD  # noqa: E402

os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://lexloom_app:app@localhost:5433/lexloom"
)
# Carries the password `docker-compose.yml` starts Redis with, because that
# Redis now requires one: it is published on the host's loopback and the
# deployment host is shared, so "only 127.0.0.1" was never the access control it
# was written up as (app/redis_auth.py). A developer running the suite against
# some other Redis exports REDIS_URL and everything below follows it.
os.environ.setdefault("REDIS_URL", f"redis://:{DEV_REDIS_PASSWORD}@localhost:6379/0")
os.environ.setdefault("ENV", "ci")

# The demo-login rate limit and the stream ceilings are OFF for this suite, and
# this line is the whole mechanism — there is no second way to disable them.
#
# Assigned rather than `setdefault`, unlike everything above it. The suite
# creates well over a hundred organisations from 127.0.0.1, which is one
# address and therefore one budget, so a developer who happens to export
# LIMITS_ENABLED=true would watch two thirds of the run fail on a limit that is
# working correctly. Whether the suite passes must not depend on the shell it
# was started from.
#
# What keeps this from being a way to disable the ceilings in production is not
# discipline: `Settings` refuses to construct with `limits_enabled=False` when
# `ENV=production`, so the process does not start. See
# test_the_ceilings_cannot_be_switched_off_in_production.
os.environ["LIMITS_ENABLED"] = "false"

def redis_url_on_database(url: str, database: int) -> str:
    """Same server, same credentials, a different logical database."""
    return urlunsplit(urlsplit(url)._replace(path=f"/{database}"))


# Rate-limit state for the tests that turn the ceilings back on. Database 1,
# not 0: the development box shares one Redis with the running demo stack and
# with Celery, both on database 0, so keys written here cannot be mistaken for
# a real visitor's counter, cannot consume a real stream slot, and can be swept
# by prefix on the way out without touching anything that matters.
#
# Derived from `REDIS_URL` rather than written out, for the same reason
# `WORKER_DATABASE_URL` is derived from `DATABASE_URL` below: the host, the port
# and — now that the Compose instance requires AUTH — the password have to
# follow wherever the suite is pointed. A literal here would authenticate
# against the default while everything else in the run talked to the exported
# server, and the failure would name Redis rather than this line.
LIMITS_TEST_REDIS_URL = redis_url_on_database(os.environ["REDIS_URL"], 1)

# The worker connects as lexloom_worker, which holds grants lexloom_app
# deliberately does not (EXECUTE on the purge function, migration 0004). Derived
# from DATABASE_URL so it follows the host, port and database wherever the suite
# runs — locally against Compose on 5433, in CI against the service container on
# 5432.
#
# Parsed and rebuilt rather than string-replaced. A `.replace("lexloom_app:app",
# …)` matches only the default password, so the moment APP_DB_PASSWORD is
# customised it silently returns the URL unchanged and every "the worker can do
# this" test runs as lexloom_app. It would fail — but on the grant, pointing the
# reader at migration 0004 instead of at this line.
_WORKER_ROLE = "lexloom_worker"


def worker_dsn_from(app_dsn: str, password: str) -> str:
    """Swap the role and password out of an application DSN, keep everything else."""
    return (
        make_url(app_dsn)
        .set(username=_WORKER_ROLE, password=password)
        .render_as_string(hide_password=False)
    )


WORKER_DATABASE_URL = os.environ.get("WORKER_DATABASE_URL") or worker_dsn_from(
    os.environ["DATABASE_URL"], os.environ.get("WORKER_DB_PASSWORD", "worker")
)

# Every table in `public` carrying an `org_id` column is org-scoped. Discovered
# from the catalog rather than listed, so a table added tomorrow is covered
# without editing a literal — and lives here rather than in either test module,
# because two modules assert different things about the same set and a second
# copy of this query would let them disagree silently.
ORG_ID_TABLES_SQL = """
SELECT DISTINCT c.relname
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
WHERE a.attname = 'org_id'
  AND a.attnum > 0
  AND NOT a.attisdropped
  AND c.relkind = 'r'
  AND c.relnamespace = 'public'::regnamespace
"""

from app.chat.embedding import cache_dir, model_is_cached  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.session import system_session, tenant_session  # noqa: E402
from app.main import create_app  # noqa: E402
from app.worker.celery_app import celery_app  # noqa: E402


def pytest_sessionstart(session):
    """Fail once, clearly, when the local Redis predates `requirepass`.

    `docker-compose.yml` started requiring a password on Redis, because the
    deployment host is shared and "published on 127.0.0.1 only" was never the
    access control it had been written up as. A container created before that
    change is still running without one, and every process that reaches it
    keeps working — so the mismatch is invisible until the suite runs.

    What it looks like without this hook, measured: eleven failures and six
    errors spread across `test_limits.py`, `test_ingest_api.py` and
    `test_ingest_job.py`, each one a `capacity_unavailable` raised by the
    limits backend's own error path, with `AUTH <password> called without any
    password configured for the default user` buried in a captured log. Every
    one of those tests is fine. Nothing in that output names the container.

    Checked here rather than in a test because it is not a property of the
    code: it is a statement about the machine the suite is running on, and the
    right response is to recreate one container, not to change anything in this
    repository. Recreating it also restarts whatever is connected to it, which
    is why this refuses rather than fixing it — on a shared box that decision
    belongs to a person.
    """
    import redis as _redis

    url = os.environ["REDIS_URL"]
    try:
        _redis.Redis.from_url(url).ping()
    except _redis.exceptions.AuthenticationError as exc:
        if "without any password" not in str(exc):
            raise
        pytest.exit(
            "The Redis at REDIS_URL is running without a password, but this suite "
            "sends one because docker-compose.yml now requires it.\n\n"
            "  docker compose up -d --force-recreate redis\n\n"
            "That restarts every container connected to it, so do it deliberately: "
            "on this box the api and worker containers hold connections to the same "
            "Redis and will need recreating too.\n\n"
            "To run against a Redis you do not want to touch, export a URL without "
            "credentials -- but note that test_the_suite_reaches_redis_with_a_password_too "
            "will then fail on purpose, because the authenticated path is the one "
            "production uses and a green suite must not hide that it went untested:\n\n"
            "  REDIS_URL=redis://localhost:6379/0 pytest",
            returncode=1,
        )
    except (_redis.exceptions.ConnectionError, OSError):
        # Not this hook's business. The tests that need Redis report it
        # themselves, and the ones that do not should still run.
        pass


def pytest_collection_modifyitems(config, items):
    """Handle a cold model cache: skip what can be skipped, refuse what cannot.

    CI installs dependencies onto a bare runner: there is no image and, on a
    cold cache, no model. Without this the suite would download ~90 MB per run
    to re-establish that 384 is still 384, and would go red for a network
    failure that has nothing to do with the code. (fastembed tries HuggingFace
    and then a GCS tarball, so it takes losing both, not just one.)
    `actions/cache` restores the weights when it can; when it cannot, the
    `model` tests are skipped rather than slow.

    Skipping rather than failing for those, because an absent model is not a
    defect in anything — and because the two properties at stake are each also
    covered without it: the dimension against the ORM column, the loading
    discipline against the source of `signals.py`. What only the real model can
    prove is that those two agree with the weights actually shipped, which is
    worth running where the weights exist and worth nothing where they do not.

    The `live_server` tests are the exception and get the opposite treatment —
    see below.
    """
    if model_is_cached():
        return

    # A test that starts a real uvicorn cannot be handled by skipping, because
    # nothing about it is marked or optional: app/main.py's lifespan warms the
    # embedder before the port opens, so on a cold cache the download consumes
    # the fixture's 30 s readiness budget and the run dies with "uvicorn never
    # became live" — a message naming neither the model nor the download. That
    # cost thirty seconds and a wrong hypothesis when it was first hit.
    #
    # Refuse the run instead, in under two seconds, naming the cause and the
    # command. Deliberately NOT downloading here: a test run that silently
    # pulls 90 MB is the other half of what this hook exists to prevent, and
    # the developer, not the suite, should decide when to spend that.
    #
    # Narrow on purpose. It cannot fire in CI, where the workflow's "Fetch the
    # embedding model" step has already run and `model_is_cached()` is true; and
    # it cannot fire for a run whose collected tests never start a server, so
    # `pytest tests/test_embedding.py` on a cold cache still works.
    needs_live_server = [item for item in items if "live_server" in item.fixturenames]
    if needs_live_server:
        raise pytest.UsageError(
            f"The embedding model is not cached in {cache_dir()}, and "
            f"{len(needs_live_server)} test(s) in this run start a real uvicorn "
            "process whose lifespan loads it before the port opens. They would "
            "fail after 30s with 'uvicorn never became live', which names "
            "neither the model nor the download.\n\n"
            "Fetch it once (~90 MB), then re-run:\n\n"
            "    python -c 'from app.chat.embedding import get_embedder; "
            "get_embedder()._ensure()'\n"
        )

    skip = pytest.mark.skip(reason=f"ONNX model not cached in {cache_dir()}")
    for item in items:
        if "model" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
async def org_scoped_tables() -> list[str]:
    async with system_session() as session:
        tables = (await session.execute(text(ORG_ID_TABLES_SQL))).scalars().all()
    assert tables, "no org_id-scoped tables found — the discovery query itself is broken"
    return list(tables)


@pytest.fixture
async def worker_role(monkeypatch):
    """Run the code under test as lexloom_worker instead of lexloom_app.

    Not a convenience. Migration 0004 grants EXECUTE on the purge function to
    the worker only, so a purge test running under the API role would fail —
    and the fix a hurried reader reaches for is widening the grant, which hands
    an attacker who reaches SQL through the API exactly the destructive
    capability it was narrowed to withhold. Patching the engine keeps the
    production grant shape and makes the test exercise it.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    worker_engine = create_async_engine(WORKER_DATABASE_URL, pool_size=1, max_overflow=0)
    # Assert the role here, where a mis-derived DSN is one line away, rather than
    # letting it surface later as a permission error that reads like a missing
    # grant in migration 0004.
    async with worker_engine.connect() as conn:
        connected_as = await conn.scalar(text("SELECT current_user"))
    assert connected_as == _WORKER_ROLE, (
        f"worker_role fixture connected as {connected_as!r}, not {_WORKER_ROLE!r} — "
        "WORKER_DATABASE_URL was derived wrongly, the grants are fine"
    )
    monkeypatch.setattr("app.db.engine.engine", worker_engine)
    monkeypatch.setattr(
        "app.db.session.session_factory",
        async_sessionmaker(worker_engine, expire_on_commit=False),
    )
    yield
    await worker_engine.dispose()


class _OwnKeysJwks:
    """A JwksCache that serves the key set of the very app it belongs to."""

    def __init__(self, idp) -> None:
        self._idp = idp

    async def get(self) -> dict:
        return self._idp.jwks()


@pytest.fixture
def app() -> FastAPI:
    """Exposed separately so tests can reach app.state (the dev IdP, the JWKS
    cache) without poking at private attributes on the HTTP client.

    The JWKS cache is replaced because the real one fetches AUTH_JWKS_URL over
    HTTP, which defaults to http://localhost:8100 — the Compose `api` container,
    a *different* process whose dev IdP generated a *different* keypair in
    memory at startup. Every token this app mints would be rejected with "no key
    matches kid", and with the container stopped the cache would fail closed
    instead. Both failures point at authentication, neither is about the code
    under test. Serving the app's own keys is what makes an authenticated test
    exercise this application rather than whatever is listening on port 8100.
    """
    app = create_app()
    app.state.jwks_cache = _OwnKeysJwks(app.state.dev_idp)
    return app


@pytest.fixture
async def client(app: FastAPI) -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest.fixture
async def created_orgs():
    """Registry of organisations a test seeded, emptied on the way out.

    The development database is long-lived and shared between runs, so a test
    that seeds a tenant and walks away changes what the *next* run measures.
    That is not hypothetical here: the demo organisations this task introduces
    carry an expiry, so yesterday's leftovers are today's already-expired rows
    and any purge count computed over them is ambient rather than specific.

    Removal goes through each organisation's own tenant_session — the only
    context its RLS policies admit — and takes the child rows first, because
    this schema has no foreign keys and therefore no cascade.
    """
    registry: list[str] = []
    yield registry
    for org_id in registry:
        async with tenant_session(org_id) as session:
            for statement in (
                "DELETE FROM messages WHERE org_id = :org",
                "DELETE FROM document_chunks WHERE org_id = :org",
                "DELETE FROM conversations WHERE org_id = :org",
            ):
                await session.execute(text(statement), {"org": org_id})
            await session.execute(
                text("DELETE FROM organizations WHERE id = :org"), {"org": org_id}
            )


@pytest.fixture
async def two_orgs(created_orgs: list[str]) -> tuple[str, str]:
    """Seed two organisations, each owning one chunk whose text names it.

    Here rather than in a test module because two modules need it, and a second
    copy would be a second account of what a seeded tenant looks like — the two
    could drift while both stayed green.

    `organizations` carries RLS too (migration 0003), but its INSERT policy is
    unconditional (`WITH CHECK (true)`) — anyone can create an organisation,
    including a caller with no tenant context yet, which is exactly the shape of
    the demo-login endpoint. So this insert goes through system_session.
    `document_chunks` has no such unconditional policy: the application role is
    neither the table owner nor BYPASSRLS, so the policy binds it. On SELECT an
    unset context silently matches zero rows; on INSERT Postgres evaluates
    WITH CHECK and *raises*. Chunks therefore have to be written through
    tenant_session.

    Registered for teardown, which it was not while it lived in a test module.
    That omission is measurable rather than theoretical: the development
    database is shared between runs and had accumulated over a thousand
    organisations by the time anyone counted.
    """
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    async with system_session() as session:
        for org_id, name in ((a, "Acme Legal"), (b, "Globex Compliance")):
            await session.execute(
                text("INSERT INTO organizations (id, external_id, name) VALUES (:i, :e, :n)"),
                {"i": org_id, "e": f"ext_{org_id}", "n": name},
            )
    created_orgs.extend([a, b])

    for org_id, name in ((a, "Acme Legal"), (b, "Globex Compliance")):
        async with tenant_session(org_id) as session:
            await session.execute(
                text(
                    "INSERT INTO document_chunks (id, org_id, content, page_from, page_to) "
                    "VALUES (:i, :o, :c, 1, 1)"
                ),
                {"i": str(uuid.uuid4()), "o": org_id, "c": f"secret text of {name}"},
            )
    return a, b


@pytest.fixture
async def demo_auth(client: AsyncClient, created_orgs: list[str]) -> dict[str, str]:
    """Log in as a throwaway organisation and return the Authorization header."""
    body = (
        await client.post("/api/v1/auth/demo-login", json={"label": "Acme Legal"})
    ).json()
    created_orgs.append(body["org_id"])
    return {"Authorization": f"Bearer {body['access_token']}"}


@pytest.fixture
async def demo_org_id(demo_auth: dict[str, str], created_orgs: list[str]) -> str:
    """The organisation `demo_auth` is logged into, for tests that also read rows.

    Taken from the teardown registry rather than decoded out of the token: it is
    the same value the login returned, and depending on `demo_auth` is what
    makes the last entry that login's.
    """
    return created_orgs[-1]


@pytest.fixture
def settings_override():
    """Export settings for one test and put the environment back afterwards.

    Not `monkeypatch.setenv`, for one reason that matters: the settings cache
    has to be cleared *after* the environment is restored, and monkeypatch's
    own undo runs after the fixture that requested it. Clearing first would let
    the next `get_settings()` repopulate the cache from this test's variables.

    Each keyword is a `Settings` field, exported upper-cased — which is exactly
    how pydantic-settings names the environment variable for it, so a
    misspelled field name fails as an unknown setting rather than silently
    doing nothing.
    """
    saved: dict[str, str | None] = {}

    def _export(**values: object) -> None:
        for field, value in values.items():
            name = field.upper()
            saved.setdefault(name, os.environ.get(name))
            os.environ[name] = str(value)
        get_settings.cache_clear()

    yield _export

    for name, previous in saved.items():
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous
    get_settings.cache_clear()


@pytest.fixture
async def limits_on(settings_override):
    """Turn the ceilings back on for one test, with numbers of its own.

    **Request this fixture before `client` and before anything that logs in.**
    `create_app` is not the problem — the limiter reads `get_settings()` per
    request — but `demo_auth` performs a demo-login, and a login made before
    the fixture has raised the ceilings is metered against the suite's default
    settings and against database 0.

    Call it: `limits_on(demo_login_limit=2)`. Environment handling and the
    settings cache belong to `settings_override`; this fixture adds the two
    variables every such test needs and the Redis sweep on the way out.

    The sweep is scoped to `KEY_PREFIX` on database 1 — never a FLUSHDB, and
    never database 0, which belongs to the running stack. It runs before
    `settings_override` restores `REDIS_URL`, because fixtures are finalised in
    reverse dependency order.
    """
    configured = False

    def _configure(**overrides: object) -> None:
        nonlocal configured
        configured = True
        settings_override(
            limits_enabled="true", redis_url=LIMITS_TEST_REDIS_URL, **overrides
        )

    yield _configure

    if configured:
        import redis.asyncio as aioredis

        from app.limits import KEY_PREFIX

        async with aioredis.from_url(LIMITS_TEST_REDIS_URL) as redis:
            async for key in redis.scan_iter(match=f"{KEY_PREFIX}*"):
                await redis.delete(key)


@pytest.fixture
def celery_eager():
    celery_app.conf.task_always_eager = True
    yield
    celery_app.conf.task_always_eager = False


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    """Each test gets its own event loop; the engine's pool must not outlive it.

    A connection created on a previous test's loop and handed to the next one
    fails in ways that point at the wrong code entirely.
    """
    yield
    from app.db.engine import engine

    await engine.dispose()

# Whether this session has already established that the template organisation
# holds the catalogue corpus. Module state rather than a session-scoped fixture
# on purpose: every test here gets its own event loop (see
# `_dispose_engine_between_tests`), and a session-scoped async fixture would
# hold a connection created on a loop that is gone by the second test.
_catalogue_template_checked = False

@pytest.fixture(autouse=True)
async def _the_catalogue_template_exists():
    """Provisioning copies the catalogue corpus, so the template must be there.

    Since every demo tenant is born holding the whole GDPR, `provision_demo_org`
    reads it out of the public organisation — and refuses to commit a tenant
    without it. On a database where `scripts/seed_corpus.py` has never run, that
    turns roughly forty tests that have nothing to do with ingestion into
    failures inside demo-login.

    So the suite loads it, once, if it is missing. In a subprocess and not in
    process: the load parses and embeds 978 chunks, and doing that on a test's
    own event loop would hand the engine's pool to a loop that is disposed at
    the end of the test.
    """
    global _catalogue_template_checked
    if not _catalogue_template_checked:
        from sqlalchemy import text as _text

        from app.chat.corpus import PUBLIC_CORPUS_ORG_ID, SEED_SOURCE_KEY
        from app.db.session import tenant_session

        async with tenant_session(str(PUBLIC_CORPUS_ORG_ID)) as session:
            held = await session.scalar(
                _text(
                    "SELECT count(*) FROM document_chunks "
                    "WHERE org_id = :org AND source_key <> :seed"
                ),
                {"org": str(PUBLIC_CORPUS_ORG_ID), "seed": SEED_SOURCE_KEY},
            )
        if not held:
            _load_catalogue_template()
        _catalogue_template_checked = True
    yield

def _load_catalogue_template() -> None:
    """Run the deployment's own seeder, so the suite cannot drift from it."""
    import subprocess  # noqa: PLC0415 - only needed on a cold database
    import sys  # noqa: PLC0415

    root = pathlib.Path(__file__).resolve().parents[1]
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell, no input
        [sys.executable, str(root / "scripts" / "seed_corpus.py")],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "could not load the catalogue template corpus that provisioning "
            f"copies from:\n{result.stdout}\n{result.stderr}"
        )
