import uuid

import pytest
from sqlalchemy import text

from app.db.session import system_session, tenant_session

ORG_SCOPED_TABLES = ["conversations", "messages", "document_chunks"]


@pytest.fixture
async def two_orgs() -> tuple[str, str]:
    """Seed two organisations, each owning one chunk.

    `organizations` carries no RLS, so it is written through system_session.
    `document_chunks` does, and the application role is neither the table owner
    nor BYPASSRLS — so the policy binds it unconditionally. On SELECT an unset
    context silently matches zero rows; on INSERT Postgres evaluates WITH CHECK
    and *raises*. Chunks therefore have to be written through tenant_session.
    """
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    async with system_session() as s:
        for org_id, name in ((a, "Acme Legal"), (b, "Globex Compliance")):
            await s.execute(
                text("INSERT INTO organizations (id, external_id, name) VALUES (:i, :e, :n)"),
                {"i": org_id, "e": f"ext_{org_id}", "n": name},
            )

    for org_id, name in ((a, "Acme Legal"), (b, "Globex Compliance")):
        async with tenant_session(org_id) as s:
            await s.execute(
                text(
                    "INSERT INTO document_chunks (id, org_id, content, page_from, page_to) "
                    "VALUES (:i, :o, :c, 1, 1)"
                ),
                {"i": str(uuid.uuid4()), "o": org_id, "c": f"secret text of {name}"},
            )
    return a, b


@pytest.mark.parametrize("table", ORG_SCOPED_TABLES)
async def test_every_org_scoped_table_has_rls_forced(table: str) -> None:
    """Queried from the catalog, so a table added tomorrow is covered automatically."""
    async with system_session() as s:
        row = (
            await s.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE oid = to_regclass(:t)"
                ),
                {"t": f"public.{table}"},
            )
        ).one()
    assert row.relrowsecurity, f"{table} has RLS disabled"
    assert row.relforcerowsecurity, (
        f"{table} lacks FORCE ROW LEVEL SECURITY — the owner would bypass its own policy"
    )


async def test_a_query_with_no_where_clause_returns_only_the_caller_org(two_orgs) -> None:
    org_a, org_b = two_orgs
    async with tenant_session(org_a) as s:
        rows = (await s.execute(text("SELECT org_id FROM document_chunks"))).scalars().all()
    assert rows, "org A should see its own chunk"
    assert all(str(r) == org_a for r in rows)
    assert org_b not in {str(r) for r in rows}


async def test_a_session_without_tenant_context_sees_nothing(two_orgs) -> None:
    """Fail closed. A policy written as `current_setting(...) IS NULL OR ...`
    would return every row here — that is the bypass to never write."""
    async with system_session() as s:
        await s.execute(text("SET LOCAL ROLE lexloom_app"))
        rows = (await s.execute(text("SELECT id FROM document_chunks"))).scalars().all()
    assert rows == []


async def test_insert_stamped_with_another_org_is_rejected(two_orgs) -> None:
    """Without an explicit WITH CHECK, org A could write rows owned by org B."""
    org_a, org_b = two_orgs
    with pytest.raises(Exception, match="row-level security"):
        async with tenant_session(org_a) as s:
            await s.execute(
                text(
                    "INSERT INTO document_chunks (id, org_id, content, page_from, page_to) "
                    "VALUES (:i, :o, 'smuggled', 1, 1)"
                ),
                {"i": str(uuid.uuid4()), "o": org_b},
            )
