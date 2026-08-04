"""count_orgs_with_corpus: a bounded SECURITY DEFINER count for INGEST_ORG_CAP

Revision ID: 0007

`INGEST_ORG_CAP` (app/config.py) needs a count across every tenant: how many
organisations hold an ingested corpus right now. `lexloom_app` does not own
`document_chunks`, FORCE RLS applies, and `system_session()`
(app/db/session.py) sets no GUC — so an unscoped `SELECT count(*)` under it
evaluates the tenant policy's qual against `NULL` and returns 0, always. A cap
read that way never trips: the obvious implementation is a ceiling that looks
wired in and is not. This function is the same move migration 0004 makes for
`purge_expired_orgs`, applied to a read instead of a delete.

Two departures from a naive definer function, both load-bearing.

`SET row_security = off`. The FORCE comments in 0002/0003 promise a
deployment where the migration role owns the tables but is not a superuser.
On that shape RLS binds the owner too, no policy grants the owner anything,
and this function returns 0 forever — reintroducing, one layer down and just
as silently, the exact failure this function exists to prevent. With the
directive it raises instead: loud beats quiet, the same trade 0004 makes.

`source_key <> 'seed:gdpr-excerpts'`. Every organisation is given the seed
corpus at login (`app.chat.corpus.copy_corpus_into`), and Task 22 stamped it
with `SEED_SOURCE_KEY = 'seed:gdpr-excerpts'`. Without the exclusion, every
demo organisation counts from the moment it logs in and the cap refuses the
very first real ingestion the demo ever attempts — a ceiling that disables
itself. `test_a_fresh_demo_org_does_not_count_against_the_ingest_cap` is what
holds this literal to that constant; nothing else could notice them drifting
apart, because they live in two different languages (SQL here, Python there).

Returns a count, never a row and never an id. The same argument migration
0004 makes for its own integer return (0004:19-27): an organisation id *is*
the RLS credential, and a function that handed one out would let any caller
of this function `set_config('app.current_org_id', ...)` onto a tenant it was
never scoped to. A count unlocks nothing, however often it is called — that
is the whole boundary of the privilege this migration grants.

`TO lexloom_app`, not `lexloom_worker`. The cap is enforced in the admission
path — the API, before a task is even queued (`app.ingest.jobs.
ensure_ingest_admitted`) — because a worker that discovers the ceiling after
the task is already in the queue has nothing useful to do about it. Granting
the worker role too would be a wider surface for zero benefit.

Every operator and function call below is schema-qualified for the reason
0004 gives at length: SECURITY DEFINER runs as the owner (the Compose
bootstrap superuser), the pinned `search_path` is the primary defence, and it
is one `ALTER FUNCTION` away from being reordered — at which point an
unqualified `<>` could resolve to a hostile `public.<>` instead of the
built-in operator. Qualifying every symbol removes that whole failure mode
rather than trusting the search_path alone to hold.

`STABLE`, not `VOLATILE`: this function only reads, once, within whatever
snapshot its single statement takes. `purge_expired_orgs` needs `VOLATILE`
because its second statement must see the first one's own writes; this
function performs no writes at all.
"""

from alembic import op

revision = "0007"
down_revision = "0006"


COUNT_ORGS_WITH_CORPUS_FUNCTION = """
CREATE FUNCTION public.count_orgs_with_corpus() RETURNS bigint
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $fn$
    SELECT pg_catalog.count(DISTINCT org_id)
      FROM public.document_chunks
     WHERE source_key IS NOT NULL
       AND source_key OPERATOR(pg_catalog.<>) 'seed:gdpr-excerpts'
$fn$
"""


def upgrade() -> None:
    op.execute(COUNT_ORGS_WITH_CORPUS_FUNCTION)
    # New functions are executable by PUBLIC by default. Revoke before
    # granting, or the explicit grant below is decoration.
    op.execute("REVOKE ALL ON FUNCTION public.count_orgs_with_corpus() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION public.count_orgs_with_corpus() TO lexloom_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.count_orgs_with_corpus()")
