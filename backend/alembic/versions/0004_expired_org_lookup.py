"""bounded cross-organisation lookup for the demo-org purge

Revision ID: 0004
"""

from alembic import op

revision = "0004"
down_revision = "0003"


def upgrade() -> None:
    # The purge has to answer one question no tenant context can answer:
    # "which organisations have expired?". Migration 0003 scoped every read of
    # `organizations` to `id = current_setting('app.current_org_id')`, so an
    # unscoped session sees zero rows and the obvious purge silently deletes
    # nothing.
    #
    # Three ways to buy that answer, and only one of them is cheap:
    #
    #   * a BYPASSRLS or superuser DSN for the worker — assert_unprivileged_role
    #     refuses to boot on one, deliberately, and it would hand the purge
    #     read/write reach over every live tenant to solve a problem about
    #     dead ones;
    #   * a second, permissive SELECT policy on organizations guarded by
    #     `expires_at < now()` — permissive policies OR together, so every
    #     tenant would gain standing read access to every expired tenant's
    #     label in the one table that names the tenants;
    #   * this: one argument-less, read-only function that returns opaque ids
    #     and nothing else. Every policy on organizations stays exactly
    #     `id = current_setting(...)`, and the purge deletes each expiring
    #     organisation from inside that organisation's own tenant context —
    #     the least privilege that can do the job.
    #
    # SECURITY DEFINER is what makes it work and also what makes it dangerous:
    # the owner here is the bootstrap superuser, so the pinned search_path is
    # load-bearing. Without it, anyone able to create an object in a schema
    # ahead of pg_catalog decides what `now()` means inside a superuser
    # context. The body is schema-qualified as well, so `public` appearing in
    # the path is belt-and-braces rather than the thing being trusted.
    #
    # Postgres does not inline SECURITY DEFINER SQL functions, which is the
    # property the whole design rests on: inlined, the body would be evaluated
    # under the *caller's* RLS context and return nothing.
    op.execute(
        """
        CREATE FUNCTION public.expired_org_ids() RETURNS SETOF uuid
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT id FROM public.organizations
            WHERE expires_at IS NOT NULL AND expires_at < now()
        $$
        """
    )
    # New functions are executable by PUBLIC by default. Revoke before
    # granting, or the explicit grant below is decoration.
    op.execute("REVOKE ALL ON FUNCTION public.expired_org_ids() FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.expired_org_ids() TO lexloom_app, lexloom_worker"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.expired_org_ids()")
