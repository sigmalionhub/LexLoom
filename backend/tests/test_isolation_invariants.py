import uuid

import pytest
from sqlalchemy import text

from app.db.session import system_session, tenant_session

# Every table in `public` carrying an `org_id` column is expected to carry
# forced RLS with an explicit `USING`/`WITH CHECK` policy. The set comes from
# the `org_scoped_tables` fixture in conftest, which discovers it from the
# catalog — so a table added tomorrow is covered automatically, and the purge
# tests in test_demo_orgs.py measure their coverage against the same set rather
# than a second copy of it that could drift.
#
# `organizations` is deliberately outside this set: its tenant column is its
# own primary key (`id`), not `org_id`, and its policy shape is four
# single-command policies rather than one `FOR ALL`. It gets its own tests
# below.
FORCE_MESSAGE = (
    "lacks FORCE ROW LEVEL SECURITY — FORCE binds a non-superuser table owner; "
    "in this stack the migration role is the bootstrap superuser and bypasses "
    "RLS regardless, but FORCE is what keeps the policy binding once that is fixed"
)


@pytest.fixture
async def two_orgs() -> tuple[str, str]:
    """Seed two organisations, each owning one chunk.

    `organizations` now carries RLS too (see migration 0003), but its INSERT
    policy is unconditional (`WITH CHECK (true)`) — anyone can create an
    organisation, including a caller with no tenant context yet, which is
    exactly the shape of the demo-login endpoint. So this insert still goes
    through system_session. `document_chunks` has no such unconditional
    policy: the application role is neither the table owner nor BYPASSRLS, so
    the policy binds it unconditionally. On SELECT an unset context silently
    matches zero rows; on INSERT Postgres evaluates WITH CHECK and *raises*.
    Chunks therefore have to be written through tenant_session.
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


async def test_every_org_scoped_table_has_rls_forced(org_scoped_tables) -> None:
    """Catalog-driven: finds every table with an org_id column rather than
    naming them, so a table added tomorrow is covered automatically."""
    async with system_session() as s:
        for table in org_scoped_tables:
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
            assert row.relforcerowsecurity, f"{table} {FORCE_MESSAGE}"


async def test_organizations_has_rls_forced() -> None:
    """organizations is excluded from the org_id-column catalog query above
    because its tenant column is its own primary key — it needs this table
    named explicitly rather than being swept up automatically."""
    async with system_session() as s:
        row = (
            await s.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE oid = to_regclass('public.organizations')"
                )
            )
        ).one()
    assert row.relrowsecurity, "organizations has RLS disabled"
    assert row.relforcerowsecurity, f"organizations {FORCE_MESSAGE}"


async def test_organizations_policies_cover_every_command_without_an_is_null_bypass() -> None:
    """organizations' four single-command policies don't fit the FOR ALL shape
    the pg_policies test below checks, so they get their own coverage here:
    every command is policied, carries the expression Postgres requires for
    that command, and none of those expressions contain an IS NULL bypass.
    """
    # (needs a USING expression, needs a WITH CHECK expression) per command —
    # Postgres only attaches USING to SELECT/UPDATE/DELETE and WITH CHECK to
    # INSERT/UPDATE, so "both non-null" does not apply per-policy here.
    expected = {
        "INSERT": (False, True),
        "SELECT": (True, False),
        "UPDATE": (True, True),
        "DELETE": (True, False),
    }
    async with system_session() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT cmd, qual, with_check FROM pg_policies "
                    "WHERE schemaname = 'public' AND tablename = 'organizations'"
                )
            )
        ).all()
    seen = {row.cmd for row in rows}
    assert seen == set(expected), f"organizations policies do not cover every command: {seen}"
    for cmd, qual, with_check in rows:
        needs_using, needs_check = expected[cmd]
        if needs_using:
            assert qual is not None, f"organizations {cmd} policy has no USING expression"
            assert "IS NULL" not in qual.upper(), (
                f"organizations {cmd} USING has an IS NULL bypass"
            )
        if needs_check:
            assert with_check is not None, (
                f"organizations {cmd} policy has no WITH CHECK expression"
            )
            assert "IS NULL" not in with_check.upper(), (
                f"organizations {cmd} WITH CHECK has an IS NULL bypass"
            )


async def test_org_scoped_policies_have_no_gaps_and_no_is_null_bypass(org_scoped_tables) -> None:
    """Inspect pg_policies directly rather than only the two RLS flags.

    A deleted policy leaves relrowsecurity/relforcerowsecurity untouched (RLS
    would then default-deny every row, which happens to look safe — but a
    later change that swaps FORCE off, or a role that gains BYPASSRLS, would
    turn a deleted policy into a silent bypass with nothing here to catch it).
    A `current_setting(...) IS NULL OR org_id = ...` bypass is invisible to
    the flags-only test too, since RLS is still enabled and forced; only the
    policy expression itself reveals it.
    """
    async with system_session() as s:
        for table in org_scoped_tables:
            rows = (
                await s.execute(
                    text(
                        "SELECT qual, with_check FROM pg_policies "
                        "WHERE schemaname = 'public' AND tablename = :t"
                    ),
                    {"t": table},
                )
            ).all()
            assert rows, f"{table} has RLS enabled but no policy at all"
            for qual, with_check in rows:
                assert qual is not None, f"{table} policy has no USING expression"
                assert with_check is not None, f"{table} policy has no WITH CHECK expression"
                assert "IS NULL" not in qual.upper(), f"{table} USING contains an IS NULL bypass"
                assert "IS NULL" not in with_check.upper(), (
                    f"{table} WITH CHECK contains an IS NULL bypass"
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


async def test_a_tenant_cannot_see_another_organisations_row(two_orgs) -> None:
    """organizations is the table that names the tenants; if it leaks, every
    other policy in this module is isolating rows nobody could have found
    anyway, because their org_id would have come from here."""
    org_a, org_b = two_orgs
    async with tenant_session(org_a) as s:
        ids = {str(i) for i in (await s.execute(text("SELECT id FROM organizations"))).scalars()}
    assert org_a in ids
    assert org_b not in ids


async def test_a_tenant_cannot_rename_another_organisation(two_orgs) -> None:
    org_a, org_b = two_orgs
    async with tenant_session(org_a) as s:
        result = await s.execute(
            text("UPDATE organizations SET name = 'renamed-by-org-a' WHERE id = :b"),
            {"b": org_b},
        )
    assert result.rowcount == 0, "org A must not be able to rename org B"
    # Verified through org B's own tenant_session: system_session has no
    # context, and organizations' SELECT policy would hide org B's row from
    # it just as readily as it hides it from org A — a system_session read
    # here would misreport an unrelated visibility gap as "the update failed
    # to apply", when what actually happened is unobservable from there.
    async with tenant_session(org_b) as s:
        name = await s.scalar(text("SELECT name FROM organizations WHERE id = :b"), {"b": org_b})
    assert name == "Globex Compliance"


async def test_a_tenant_cannot_delete_another_organisation(two_orgs) -> None:
    org_a, org_b = two_orgs
    async with tenant_session(org_a) as s:
        result = await s.execute(text("DELETE FROM organizations WHERE id = :b"), {"b": org_b})
    assert result.rowcount == 0, "org A must not be able to delete org B"
    async with tenant_session(org_b) as s:
        still_there = await s.scalar(
            text("SELECT count(*) FROM organizations WHERE id = :b"), {"b": org_b}
        )
    assert still_there == 1


async def test_creating_an_organisation_still_works() -> None:
    """The demo-login endpoint creates an organisation for a visitor who has
    no tenant context yet, so INSERT must stay unconditional.

    Verified through the new org's own tenant_session rather than
    system_session: system_session has no context, so organizations' SELECT
    policy would hide the very row this test just inserted, making a
    successful creation look like a failed one.
    """
    new_org = str(uuid.uuid4())
    async with system_session() as s:
        await s.execute(
            text("INSERT INTO organizations (id, external_id, name) VALUES (:i, :e, :n)"),
            {"i": new_org, "e": f"ext_{new_org}", "n": "Brand New Tenant"},
        )
    async with tenant_session(new_org) as s:
        name = await s.scalar(
            text("SELECT name FROM organizations WHERE id = :i"), {"i": new_org}
        )
    assert name == "Brand New Tenant"
