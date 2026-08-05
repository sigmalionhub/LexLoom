"""reserve_ingest_slot: hold an INGEST_ORG_CAP slot at admission, not at commit

Revision ID: 0008

`ensure_ingest_admitted` (`app/ingest/jobs.py`) read `count_orgs_with_corpus()`
and refused when it had reached `INGEST_ORG_CAP`. That counts organisations
holding a **committed** corpus, and nothing commits during admission — so the
ceiling could be walked straight through. Measured by the reviewer, with two
free slots: **12 of 12** consecutive admissions passed, because between the
first and the twelfth no ingest had finished. `_guard_write_transaction`
re-checks liveness and the per-organisation duplicate inside the write
transaction but never the global cap, so the whole burst then committed.

The cost is not the rows. It is the queue: at a measured 75.32 s per ingest on
`--concurrency=1`, one address' demo-login budget (30/hour) buys roughly 37
minutes per hour of pinned CPU at ~450 MiB RSS on a 2-vCPU box carrying a
stranger's production — and `purge_expired_orgs` shares that queue and waits
behind all of it.

So the slot is now **taken at admission**, before a task is queued, rather than
inferred afterwards from state that does not exist yet. Three departures follow
from that, each load-bearing.

`ingest_reserved_at` on `organizations`, not a Redis key. The module docstring
in `app/ingest/jobs.py` already argues this for both ceilings and the argument
is unchanged: a counter in a 48 MB `allkeys-lru` Redis is a ceiling that
evicts itself under memory pressure with no log line. A column on the row also
means the purge already collects the reservation — deleting the organisation
deletes it — so nothing has to be reconciled. A separate table would have been
a new `org_id`-bearing table, which migration 0004's named child list and
`test_the_purge_covers_every_org_scoped_table` would both have to grow.

`pg_advisory_xact_lock`, which reverses a decision `ensure_ingest_admitted`'s
docstring took deliberately: it named the concurrent-admission race, judged it
bounded by "the number of concurrent admissions", and priced preventing it at
"a lock or a serialised admission path". The measurement above is what changes
that price — the bound was the whole queue, not a couple of tenants. The lock
is held for the two statements below and released with the transaction.

The organisation comes from `current_setting('app.current_org_id')`, not from a
parameter, so this function cannot be steered onto a tenant the caller is not
already inside. That is the same instinct as 0004's and 0007's "returns a
count, never an id", applied to a write: an organisation id *is* the RLS
credential, so a function that accepted one would let any caller of it reserve
against — and shorten the TTL of — a tenant it had never been scoped to.

The three integers it does take are the application's own numbers, and none of
them widens anything a caller does not already have. `p_cap` decides only
whether *the caller's own* organisation is admitted, and a caller able to pass
their own cap is a caller able to run arbitrary SQL as `lexloom_app`, who could
consume the slot for real by inserting chunks into their own tenant.
`p_org_ttl_seconds` reaches only a `LEAST`, so it can shorten a TTL and never
extend one. A large `p_reservation_ttl_seconds` makes other tenants' stale
reservations count for longer, which reduces the caller's own capacity. They
are parameters rather than literals because the numbers they carry belong to
`Settings` and to `app/ingest/jobs.py`, and a copy of them in SQL would be a
second place to change with nothing holding the two together.

--- Two fixes fall out of the same statement -------------------------------

**An orphaned corpus no longer consumes a slot for good.**
`count_orgs_with_corpus()` (0007) counted `DISTINCT org_id` over
`document_chunks` and never joined `organizations`, so chunks whose
organisation was gone kept a slot occupied with no way to ever release it —
`INGEST_ORG_CAP` ratcheting down permanently, which is precisely the one-way
latch Task 23 Step 5 exists to prevent. It counts organisations now, which is
what its name always claimed. (`app/ingest/jobs.py`'s `FOR KEY SHARE` closes
the last way to produce such an orphan; this makes the pre-existing ones
harmless rather than permanent.)

**An expired organisation stops holding a slot an hour before the purge runs.**
The count requires the organisation to be live. It used not to, and 0007's
argument for that was "the ceiling relaxes exactly when capacity is freed and
never before" — true of bytes in Postgres, and the wrong ledger: the purge
fires hourly, so a tenant that expired 59 minutes ago would refuse a visitor
over rows already condemned. The overshoot this admits is bounded by the cap
itself (~1.1 MB per tenant, so ~10 MB worst case on a 384 MB container) and it
is what makes the shortened TTL in `app/ingest/jobs.py` actually free capacity
when it lapses rather than at the next beat tick.

Every operator and function call is schema-qualified for the reason 0004 gives
at length. `LEAST` and `CASE` are SQL constructs rather than resolvable names,
so they have nothing to qualify.
"""

from alembic import op

revision = "0008"
down_revision = "0007"


ADD_RESERVATION_COLUMN = """
ALTER TABLE public.organizations ADD COLUMN ingest_reserved_at timestamptz
"""

# 0007's function, re-pointed at `organizations`. Same meaning as its name and
# its docstring always claimed — "how many organisations hold an ingested
# corpus" — now actually keyed on an organisation existing and being live.
# `CREATE OR REPLACE` keeps 0007's REVOKE/GRANT.
COUNT_ORGS_WITH_CORPUS_V2 = """
CREATE OR REPLACE FUNCTION public.count_orgs_with_corpus() RETURNS bigint
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $fn$
    SELECT pg_catalog.count(*)
      FROM public.organizations o
     WHERE (o.expires_at IS NULL
            OR o.expires_at OPERATOR(pg_catalog.>) pg_catalog.now())
       AND EXISTS (
            SELECT 1
              FROM public.document_chunks c
             WHERE c.org_id OPERATOR(pg_catalog.=) o.id
               AND c.source_key IS NOT NULL
               AND c.source_key OPERATOR(pg_catalog.<>) 'seed:gdpr-excerpts')
$fn$
"""

# 0007's function body, for `downgrade()`.
COUNT_ORGS_WITH_CORPUS_V1 = """
CREATE OR REPLACE FUNCTION public.count_orgs_with_corpus() RETURNS bigint
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

# The advisory-lock key is an arbitrary constant that only has to be the same
# one for every caller of this function; it is written as a literal rather than
# hashed from a string so a reader can see there is exactly one.
RESERVE_INGEST_SLOT = """
CREATE FUNCTION public.reserve_ingest_slot(
    p_cap integer,
    p_reservation_ttl_seconds integer,
    p_org_ttl_seconds integer
) RETURNS boolean
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $fn$
DECLARE
    -- NULLIF, LEAST and CASE are SQL constructs rather than resolvable names,
    -- so unlike every function call around them they have no schema to pin.
    v_org uuid := NULLIF(
        pg_catalog.current_setting('app.current_org_id', true), '')::uuid;
    v_stale timestamptz := pg_catalog.now() OPERATOR(pg_catalog.-)
        pg_catalog.make_interval(secs => p_reservation_ttl_seconds);
    v_holds_slot boolean;
    v_held bigint;
BEGIN
    IF v_org IS NULL THEN
        RAISE EXCEPTION 'reserve_ingest_slot requires a tenant context';
    END IF;

    -- Everything below is a read followed by a write, and the whole point is
    -- that no second admission may read between them.
    PERFORM pg_catalog.pg_advisory_xact_lock(2300008);

    -- NULL (no such organisation, or already expired) is distinguishable from
    -- false (alive, holding no reservation), and the two answers differ.
    SELECT o.ingest_reserved_at IS NOT NULL
       AND o.ingest_reserved_at OPERATOR(pg_catalog.>) v_stale
      INTO v_holds_slot
      FROM public.organizations o
     WHERE o.id OPERATOR(pg_catalog.=) v_org
       AND (o.expires_at IS NULL
            OR o.expires_at OPERATOR(pg_catalog.>) pg_catalog.now());

    IF v_holds_slot IS NULL THEN
        RETURN false;
    END IF;

    -- An organisation already holding a slot is re-admitted without consulting
    -- the cap, because it is already inside it: a retry after a failed job
    -- must not be refused by a ceiling the retrying tenant is itself part of.
    IF NOT v_holds_slot THEN
        SELECT public.count_orgs_with_corpus()
             OPERATOR(pg_catalog.+)
             (SELECT pg_catalog.count(*)
                FROM public.organizations r
               WHERE r.ingest_reserved_at IS NOT NULL
                 AND r.ingest_reserved_at OPERATOR(pg_catalog.>) v_stale
                 AND (r.expires_at IS NULL
                      OR r.expires_at OPERATOR(pg_catalog.>) pg_catalog.now()))
          INTO v_held;
        IF v_held OPERATOR(pg_catalog.>=) p_cap THEN
            RETURN false;
        END IF;
    END IF;

    -- The two terms above cannot double-count, because the write transaction
    -- clears `ingest_reserved_at` in the same transaction as its INSERT
    -- (`app/ingest/jobs.py`): a tenant holds a reservation or a corpus, never
    -- both. That is also what keeps the corpus predicate in one function.
    UPDATE public.organizations
       SET ingest_reserved_at = pg_catalog.now(),
           -- LEAST, so this can only ever shorten. A demo tenant that starts an
           -- ingest gives its slot back hours rather than a day later; one with
           -- no expiry (the seed-corpus organisation) is not given one here.
           expires_at = CASE
               WHEN expires_at IS NULL THEN NULL
               ELSE LEAST(
                   expires_at,
                   pg_catalog.now() OPERATOR(pg_catalog.+)
                       pg_catalog.make_interval(secs => p_org_ttl_seconds))
           END
     WHERE id OPERATOR(pg_catalog.=) v_org;

    RETURN true;
END
$fn$
"""


def upgrade() -> None:
    op.execute(ADD_RESERVATION_COLUMN)
    op.execute(COUNT_ORGS_WITH_CORPUS_V2)
    op.execute(RESERVE_INGEST_SLOT)
    # New functions are executable by PUBLIC by default. Revoke before granting,
    # or the explicit grant below is decoration.
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "public.reserve_ingest_slot(integer, integer, integer) FROM PUBLIC"
    )
    # `lexloom_app` and not `lexloom_worker`, for the reason 0007 gives: the cap
    # is enforced in the admission path, in the API, before a task is queued. A
    # worker that discovered the ceiling after the task was already queued would
    # have nothing useful to do about it.
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "public.reserve_ingest_slot(integer, integer, integer) TO lexloom_app"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.reserve_ingest_slot(integer, integer, integer)")
    op.execute(COUNT_ORGS_WITH_CORPUS_V1)
    op.execute("ALTER TABLE public.organizations DROP COLUMN IF EXISTS ingest_reserved_at")
