"""reserve_ingest_slot: count a tenant once, and never refuse one already inside

0008 built `reserve_ingest_slot` on a stated precondition, and wrote it down in
its own comment: *"The two terms above cannot double-count, because the write
transaction clears `ingest_reserved_at` in the same transaction as its INSERT: a
tenant holds a reservation or a corpus, never both."*

Per-source ingest breaks exactly that. A tenant that has ingested the GDPR holds
a corpus; when it starts the second document it also holds a reservation, and
for the whole duration of that job it satisfies **both** terms of the sum. Two
consequences, both wrong and both invisible without this migration:

1. **An unrelated tenant is refused while a slot is genuinely free.** With
   `INGEST_ORG_CAP = 8` and eight real tenants, the sum reads 9 and the next
   admission gets `503 capacity_unavailable`.

2. **A tenant is refused by a ceiling it is already inside.** `v_holds_slot`
   asked only about a live *reservation*, so a tenant holding a corpus and no
   reservation fell through to the cap check — and it is counted there, by the
   corpus term. When the cap is full, its own second document is refused. That
   is the precise case 0008's short-circuit exists to prevent; it just did not
   know about corpora.

Both fixes are in the counting, not in a comment. `v_holds_slot` becomes "holds
a corpus **or** a live reservation", and the reservation term excludes any
organisation the corpus term has already counted.

`CREATE OR REPLACE FUNCTION` with an unchanged signature, deliberately. A new
signature would be a new function: `PUBLIC` would get `EXECUTE` on it by
default, `lexloom_app` would get none, and the old three-argument overload would
survive unnoticed because `EXPECTED_RELATIONS` (`tests/test_demo_orgs.py`) keys
on `(schema, name, relkind)` with no argument dimension. Replacing in place
keeps 0008's `REVOKE`/`GRANT` intact, which is why this migration performs no
grant work — checked, not assumed, by
`test_the_admission_function_is_still_executable_by_the_api_role`.
"""

from alembic import op

revision = "0010"
down_revision = "0009"


_RESERVE_INGEST_SLOT_V2 = """
CREATE OR REPLACE FUNCTION public.reserve_ingest_slot(
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
    v_org uuid := NULLIF(
        pg_catalog.current_setting('app.current_org_id', true), '')::uuid;
    v_stale timestamptz := pg_catalog.now() OPERATOR(pg_catalog.-)
        pg_catalog.make_interval(secs => p_reservation_ttl_seconds);
    v_alive boolean;
    v_holds_slot boolean;
    v_held bigint;
BEGIN
    IF v_org IS NULL THEN
        RAISE EXCEPTION 'reserve_ingest_slot requires a tenant context';
    END IF;

    PERFORM pg_catalog.pg_advisory_xact_lock(2300008);

    -- Liveness first and on its own, so "no such organisation" stays
    -- distinguishable from "alive, holding nothing" — 0008 folded the two
    -- together and the fold is what made the corpus case impossible to add.
    SELECT true
      INTO v_alive
      FROM public.organizations o
     WHERE o.id OPERATOR(pg_catalog.=) v_org
       AND (o.expires_at IS NULL
            OR o.expires_at OPERATOR(pg_catalog.>) pg_catalog.now());

    IF v_alive IS NULL THEN
        RETURN false;
    END IF;

    -- Holds a slot already: a live reservation, OR a corpus. The second half is
    -- new. Without it a tenant that has ingested one document is inside the
    -- ceiling for counting purposes and outside it for admission purposes,
    -- which is how it came to be refused a slot it already occupies.
    SELECT EXISTS (
               SELECT 1 FROM public.organizations r
                WHERE r.id OPERATOR(pg_catalog.=) v_org
                  AND r.ingest_reserved_at IS NOT NULL
                  AND r.ingest_reserved_at OPERATOR(pg_catalog.>) v_stale
           )
        OR EXISTS (
               SELECT 1 FROM public.document_chunks c
                WHERE c.org_id OPERATOR(pg_catalog.=) v_org
                  AND c.source_key IS NOT NULL
                  AND c.source_key OPERATOR(pg_catalog.<>) 'seed:gdpr-excerpts'
           )
      INTO v_holds_slot;

    IF NOT v_holds_slot THEN
        -- The reservation term now excludes every organisation the corpus term
        -- has already counted, so the two are disjoint by construction rather
        -- than by a precondition that a later feature can quietly retire.
        SELECT public.count_orgs_with_corpus()
             OPERATOR(pg_catalog.+)
             (SELECT pg_catalog.count(*)
                FROM public.organizations r
               WHERE r.ingest_reserved_at IS NOT NULL
                 AND r.ingest_reserved_at OPERATOR(pg_catalog.>) v_stale
                 AND (r.expires_at IS NULL
                      OR r.expires_at OPERATOR(pg_catalog.>) pg_catalog.now())
                 AND NOT EXISTS (
                         SELECT 1 FROM public.document_chunks c
                          WHERE c.org_id OPERATOR(pg_catalog.=) r.id
                            AND c.source_key IS NOT NULL
                            AND c.source_key OPERATOR(pg_catalog.<>) 'seed:gdpr-excerpts'
                     ))
          INTO v_held;
        IF v_held OPERATOR(pg_catalog.>=) p_cap THEN
            RETURN false;
        END IF;
    END IF;

    UPDATE public.organizations
       SET ingest_reserved_at = pg_catalog.now(),
           -- LEAST, so this can only ever shorten, and never lengthens a life
           -- the purge is counting on. See 0008.
           expires_at = CASE
               WHEN expires_at IS NULL THEN NULL
               ELSE LEAST(
                   expires_at,
                   pg_catalog.now() OPERATOR(pg_catalog.+)
                       pg_catalog.make_interval(secs => p_org_ttl_seconds))
           END
     WHERE id OPERATOR(pg_catalog.=) v_org;

    RETURN true;
END;
$fn$
"""

_RESERVE_INGEST_SLOT_V1 = """
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
    op.execute(_RESERVE_INGEST_SLOT_V2)


def downgrade() -> None:
    """Restore 0008's function exactly, like all nine downgrades before it.

    A first draft of this made `downgrade()` a no-op, arguing that putting back
    a function with a known double-count is worse than leaving a correct one in
    place. That argument is wrong for the same reason it was wrong when the
    0009 draft wanted its downgrade to raise: a downgrade that does not return
    the previous state is not a downgrade, and `deploy/README.md` promises one
    that works for every migration.

    It is also unnecessary. At revision 0009 there is no per-source ingest, so
    0008's stated precondition — a tenant holds a reservation or a corpus, never
    both — holds again, and its function is correct for that world.
    """
    op.execute("DROP FUNCTION IF EXISTS public.reserve_ingest_slot(integer, integer, integer)")
    op.execute(_RESERVE_INGEST_SLOT_V1)
    # DROP discarded 0008's grants along with the function, so they are restored
    # here. The upgrade path needs no such step because it replaces in place.
    op.execute(
        "REVOKE ALL ON FUNCTION public.reserve_ingest_slot(integer, integer, integer) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "public.reserve_ingest_slot(integer, integer, integer) TO lexloom_app"
    )
