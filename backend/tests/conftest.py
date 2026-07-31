import os

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url

os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://lexloom_app:app@localhost:5433/lexloom"
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("ENV", "ci")

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

from app.db.session import system_session, tenant_session  # noqa: E402
from app.main import create_app  # noqa: E402
from app.worker.celery_app import celery_app  # noqa: E402


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
