"""row-level security for organizations

Revision ID: 0003
"""

from alembic import op

revision = "0003"
down_revision = "0002"

_POLICIES = ("org_self_insert", "org_self_select", "org_self_update", "org_self_delete")


def upgrade() -> None:
    op.execute("ALTER TABLE organizations ENABLE ROW LEVEL SECURITY")
    # FORCE binds a non-superuser table owner. In this stack the migration
    # role (organizations' owner) is the Compose bootstrap superuser and
    # bypasses RLS regardless of FORCE — superuser bypass is unconditional and
    # outranks it. FORCE is still required: it is what keeps this policy
    # binding the day the migration role stops being a superuser, which is the
    # only deployment shape this table's isolation claim can honestly rely on.
    op.execute("ALTER TABLE organizations FORCE ROW LEVEL SECURITY")
    # Creating an organisation is legitimate for any caller — the demo-login
    # endpoint provisions one for every first-time visitor, before that
    # visitor has any org context to scope a policy to. Reading, changing
    # or deleting one is not legitimate unless it is the caller's own.
    op.execute(
        "CREATE POLICY org_self_insert ON organizations "
        "FOR INSERT TO lexloom_app, lexloom_worker WITH CHECK (true)"
    )
    for command in ("SELECT", "UPDATE", "DELETE"):
        op.execute(
            f"CREATE POLICY org_self_{command.lower()} ON organizations "
            f"FOR {command} TO lexloom_app, lexloom_worker "
            "USING (id = nullif(current_setting('app.current_org_id', true), '')::uuid)"
        )
    # UPDATE's policy needs WITH CHECK too, or org A could retarget its own row
    # to org B's id. ALTER POLICY adds it without recreating the USING clause
    # set above.
    op.execute(
        "ALTER POLICY org_self_update ON organizations "
        "WITH CHECK (id = nullif(current_setting('app.current_org_id', true), '')::uuid)"
    )


def downgrade() -> None:
    for policy in _POLICIES:
        op.execute(f"DROP POLICY IF EXISTS {policy} ON organizations")
    op.execute("ALTER TABLE organizations DISABLE ROW LEVEL SECURITY")
