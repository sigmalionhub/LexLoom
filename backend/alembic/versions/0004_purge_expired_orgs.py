"""the demo-org purge, as one bounded SECURITY DEFINER statement

Revision ID: 0004
"""

from alembic import op

revision = "0004"
down_revision = "0003"

# One statement, deliberately. Everything about this function is arranged so a
# reviewer can decide whether it is safe by reading it once, top to bottom.
#
# Why it exists at all: 0003 scoped every read of `organizations` to
# `id = current_setting('app.current_org_id')`, so no tenant context can answer
# "which organisations have expired?" and an unscoped DELETE matches zero rows.
# The purge needs a capability no tenant has.
#
# Why it returns an integer and never an id: an organisation id *is* the RLS
# credential. A helper that handed out expired ids would let anyone able to run
# SQL as an application role take one, `set_config('app.current_org_id', …)`,
# and then read and write that tenant's organizations, conversations, messages
# and document_chunks — every policy in 0002 and 0003 keys off exactly that
# value. Before this migration there was no enumeration path at all: an
# unscoped `SELECT count(*) FROM organizations` returns 0 and RLS suppresses
# the statistics views, so guessing a v4 UUID was the barrier. A count is not a
# credential; it unlocks nothing.
#
# That integer counts organisations and only organisations — reclaimed orphan
# rows are deliberately kept out of it. One number that sometimes means "tenants
# destroyed" and sometimes "tenants destroyed plus stray rows tidied" is a
# number nobody can act on, and a test asserting it could not tell the two
# apart.
#
# Why it deletes the organisations first and their content second, in two
# statements rather than one: the second statement is keyed on the ids the first
# one actually destroyed.
#
# Statement 1 both decides and destroys, so no organisation can expire or have
# its TTL extended between the verdict and the deletion — a DELETE takes the row
# locks itself, and under READ COMMITTED Postgres re-evaluates the predicate
# after acquiring each one, so a tenant that renews while the purge waits is
# dropped from the set rather than destroyed on a stale verdict.
#
# Statement 2 then takes a fresh snapshot (a VOLATILE function's statements do,
# unlike a STABLE one's), which is the point: a tenant that committed a
# conversation while statement 1 was blocked on its organisation's row lock is
# invisible to statement 1 and visible to statement 2. Without it the
# organisation would go and the row would stay, unreachable forever — RLS keys
# on an org id nobody holds any more, and later runs only scan `organizations`.
#
# An earlier version swept every child row whose org_id had no `organizations`
# row, justified as "safe by construction: a child is only ever created after
# its organisation". That was false. This schema has no foreign keys at all
# (deliberately — FK checks bypass RLS), so nothing enforces that ordering: a
# transaction that inserts an organisation, has a child row committed by another
# session, and only then commits, loses that child to the sweep. A live tenant's
# data, destroyed by an unfiltered cross-tenant DELETE resting on a convention.
# Keying on the purged ids makes the statement safe by identity instead — it can
# only reach tenants this run has already destroyed, whatever anyone else is
# doing.
#
# The residual, stated plainly: a child committed after statement 2 is never
# collected. Nothing can produce one today, because inserting into a child table
# requires `app.current_org_id` to be set to that organisation, which requires a
# valid token for it — and the demo token TTL (1 h) is a twenty-fourth of the
# organisation TTL (24 h), so no live token for an organisation exists by the
# time it expires. That is an argument about two constants, not about ordering,
# and test_the_demo_token_cannot_outlive_the_organisation_it_names holds them to
# it. Add a token-refresh endpoint and the argument fails; closing the window
# properly would then need a time-bounded sweep, which means `document_chunks`
# growing a `created_at` it does not have.
#
# Why every operator and function call is schema-qualified: SECURITY DEFINER
# runs as the owner, and the owner here is the Compose bootstrap superuser. The
# pinned search_path is the primary defence, but it is one `ALTER FUNCTION`
# away from being reordered, and a reordered path is not a subtle failure — a
# `public.now()` returning 'infinity' makes every live tenant look expired and
# this function then deletes all of them. Verified, not assumed: with
# `search_path = public, pg_catalog` and hostile `public.now()` and `public.<`
# installed, an unqualified body returns live tenants and this qualified form
# returns only the genuinely expired one.
#
# Why `SET row_security = off`: the FORCE comments in 0002 and 0003 promise a
# deployment where the migration role is the table owner but *not* a superuser.
# On that shape RLS applies to the owner too, no policy grants the owner
# anything, and this function would silently delete nothing forever — a purge
# that stops working the day someone follows this repo's own security advice,
# with no signal. `row_security = off` has no effect for a superuser, and on
# that future shape it turns the silent no-op into
# `ERROR: query would be affected by row-level security policy for table …`,
# with a HINT naming the fix. Loud beats quiet.
#
# The child tables are named rather than discovered. Discovery would keep a
# table added tomorrow covered automatically, but it costs dynamic SQL inside a
# superuser-owned definer function, and this is the one object in the schema
# where a reviewer must be able to see the whole blast radius in the text.
# tests/test_demo_orgs.py drives the org_id-bearing table list from the catalog
# and fails the moment this list stops matching it, so the rot is loud at the
# only moment anyone can act on it.
PURGE_FUNCTION = """
CREATE FUNCTION public.purge_expired_orgs() RETURNS integer
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $fn$
DECLARE
    purged uuid[];
BEGIN
    WITH destroyed AS (
        DELETE FROM public.organizations
         WHERE expires_at IS NOT NULL
           AND expires_at OPERATOR(pg_catalog.<) pg_catalog.now()
        RETURNING id
    )
    SELECT pg_catalog.array_agg(id) INTO purged FROM destroyed;

    IF purged IS NULL THEN
        RETURN 0;
    END IF;

    DELETE FROM public.messages        WHERE org_id OPERATOR(pg_catalog.=) ANY (purged);
    DELETE FROM public.document_chunks WHERE org_id OPERATOR(pg_catalog.=) ANY (purged);
    DELETE FROM public.conversations   WHERE org_id OPERATOR(pg_catalog.=) ANY (purged);

    RETURN pg_catalog.array_length(purged, 1);
END
$fn$
"""

def upgrade() -> None:
    op.execute(PURGE_FUNCTION)
    # New functions are executable by PUBLIC by default. Revoke before
    # granting, or the explicit grant below is decoration.
    op.execute("REVOKE ALL ON FUNCTION public.purge_expired_orgs() FROM PUBLIC")
    # The worker, and only the worker. lexloom_app serves requests and has no
    # business destroying tenants; withholding EXECUTE means an attacker who
    # reaches arbitrary SQL through the API role cannot trigger this at all.
    op.execute("GRANT EXECUTE ON FUNCTION public.purge_expired_orgs() TO lexloom_worker")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.purge_expired_orgs()")
