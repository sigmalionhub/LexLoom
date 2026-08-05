"""copy_catalogue_corpus_into, and a corpus count that ignores what it gave away

Every demo tenant is now born holding the whole GDPR, not thirty excerpts of it.
Two consequences reach the database, and only one of them is the copy.

**The copy cannot be an ordinary `INSERT ... SELECT`.** `app/chat/corpus.py`
already wrote down why, at the point where it declined to do exactly that:
`document_chunks` has FORCE row-level security and the provisioning session is
scoped to the new tenant, so the public organisation's rows *do not exist* in
that context. The statement would insert nothing, return success, and every
visitor would arrive at an empty corpus with nothing raised anywhere. So the
copy is a `SECURITY DEFINER` function with `row_security = off`, and it is
narrow at both ends: the source organisation is a literal, not a parameter, and
the destination must equal the caller's own tenant context. It can move the
public corpus into you and it can do nothing else.

Reading it out of the database rather than out of a file is not a preference.
`copy_corpus_into` runs inside `provision_demo_org`'s transaction on an API with
`--workers 1`; the thirty seeded vectors are precomputed and committed for that
reason alone. Nine hundred and seventy-eight of them is ~1.5 MB and ~375,000
Python floats per demo login, on the path that has to paint first. Inside
Postgres it is one statement and no round trip.

**The second consequence is the ingest cap.** `count_orgs_with_corpus` (0007)
and `reserve_ingest_slot` (0010, amended by 0011) both ask "does this
organisation hold a corpus" as `source_key <> 'seed:gdpr-excerpts'` — one
exclusion, because provisioning gave out one thing. It now gives out two. Left
alone, every visitor would hold a slot from birth, `INGEST_ORG_CAP` (8) would be
full once eight tenants were alive, and the second document would become
un-ingestable for everyone.

`_TEMPLATE` below is 0011's, copied verbatim with its liveness clause already
resolved to the margin form 0011's `upgrade()` installs, and the slot predicate
turned into a marker. That is not tidiness either: the first draft of this
migration retyped the function from memory and silently dropped the `LEAST(...)`
that floors `expires_at`, which is 0011's entire subject.
`test_migration_0012_downgrade_restores_0011_exactly` compares the two strings
so the copy cannot rot.

The literals are spelled in three places — here, `SEED_SOURCE_KEY`, and
`CATALOGUE["gdpr-full"].key` — and nothing but a test can hold them together.
"""

from alembic import op

revision = "0012"
down_revision = "0011"


# The public corpus organisation, `PUBLIC_CORPUS_ORG_ID` in
# `app/chat/corpus.py`: permanent, connected to nobody, and holding no privilege
# of its own — it is a template, and this function is the only thing that reads
# it across a tenant boundary.
_PUBLIC_CORPUS_ORG = "00000000-0000-0000-0000-000000000001"

_COPY_CATALOGUE_CORPUS_TEMPLATE = """
CREATE FUNCTION public.copy_catalogue_corpus_into(p_org uuid) RETURNS bigint
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $fn$
DECLARE
    v_context uuid := NULLIF(
        pg_catalog.current_setting('app.current_org_id', true), '')::uuid;
    v_written bigint;
BEGIN
    -- The whole security argument in one test. `row_security = off` makes this
    -- function able to write into any tenant; this is what stops it. A caller
    -- can only ever fill its own context, so the function adds no reach that
    -- the caller did not already have over its own rows.
    IF v_context IS NULL OR v_context OPERATOR(pg_catalog.<>) p_org THEN
        RAISE EXCEPTION
            'copy_catalogue_corpus_into writes only into the caller''s own tenant';
    END IF;

    -- Every column, named. `app/chat/corpus.py` keeps the same rule for its own
    -- INSERT and says why: a column dropped off the list does not raise, it
    -- writes NULL into every tenant's corpus. `locator` is here even though the
    -- GDPR is a PDF and carries none — the day a `.docx` is preloaded, a silent
    -- NULL locator would strip its citations of the only position they have.
    INSERT INTO public.document_chunks
        (id, org_id, content, page_from, page_to,
         source_key, chunk_ordinal, locator, embedding)
    SELECT pg_catalog.gen_random_uuid(), p_org, c.content, c.page_from, c.page_to,
           c.source_key, c.chunk_ordinal, c.locator, c.embedding
      FROM public.document_chunks c
     WHERE c.org_id OPERATOR(pg_catalog.=) '{public_corpus_org}'::uuid
       AND c.source_key OPERATOR(pg_catalog.=) 'gdpr-full'
     ORDER BY c.chunk_ordinal;

    GET DIAGNOSTICS v_written = ROW_COUNT;
    RETURN v_written;
END;
$fn$
"""


# --------------------------------------------------------------------------
# The two predicates. `_SLOT_PREDICATE_V1` is the exact string 0010 wrote and
# 0011 carried; `_V2` adds what provisioning now also hands out.
# --------------------------------------------------------------------------

_SLOT_PREDICATE_V1 = "c.source_key OPERATOR(pg_catalog.<>) 'seed:gdpr-excerpts'"
_SLOT_PREDICATE_V2 = "c.source_key NOT IN ('seed:gdpr-excerpts', 'gdpr-full')"

_COUNT_PREDICATE_V1 = "c.source_key OPERATOR(pg_catalog.<>) 'seed:gdpr-excerpts'"
_COUNT_PREDICATE_V2 = "c.source_key NOT IN ('seed:gdpr-excerpts', 'gdpr-full')"

# **0008's body, not 0007's.** 0007 created this function; 0008 replaced it,
# re-pointing the count at `organizations` so an orphaned or expired tenant's
# chunks stop holding a slot forever. The first draft of this migration rebuilt
# it from 0007 — the file whose name matches the function — and silently
# reverted that, which
# `test_an_orphaned_corpus_does_not_consume_a_slot` caught. Twice in one
# migration is a pattern, not an accident: the newest definition of a replaced
# function is never in the file named after it.
_COUNT_TEMPLATE = """
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
               AND {predicate})
$fn$
"""

# 0011's `_TEMPLATE`, byte for byte, with `{liveness_clause}` already resolved
# to `_LIVENESS_WITH_MARGIN` — the form its `upgrade()` installs and therefore
# the state this migration inherits — and the slot predicate marked. Both
# occurrences of the predicate are spelled identically upstream, which is why a
# single marker reaches both.
_RESERVE_TEMPLATE = """
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

    SELECT true
      INTO v_alive
      FROM public.organizations o
     WHERE o.id OPERATOR(pg_catalog.=) v_org
       AND (o.expires_at IS NULL
            OR o.expires_at OPERATOR(pg_catalog.>) (pg_catalog.now()
               OPERATOR(pg_catalog.+) pg_catalog.make_interval(secs => 600)));

    IF v_alive IS NULL THEN
        RETURN false;
    END IF;

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
                  AND {slot_predicate}
           )
      INTO v_holds_slot;

    IF NOT v_holds_slot THEN
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
                            AND {slot_predicate}
                     ))
          INTO v_held;
        IF v_held OPERATOR(pg_catalog.>=) p_cap THEN
            RETURN false;
        END IF;
    END IF;

    UPDATE public.organizations
       SET ingest_reserved_at = pg_catalog.now(),
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


def reserve_ingest_slot(slot_predicate: str) -> str:
    """0011's function with one predicate swapped.

    A template with a marker rather than an f-string, for 0011's own reason: an
    f-string trips ruff's S608 (SQL built by interpolation), and the honest
    answer is to stop interpolating rather than to silence the rule. There is no
    input either way — the predicate is one of the two literals above.

    Public rather than underscored so the test that compares this migration's
    downgrade against 0011's upgrade can call it.
    """
    return _RESERVE_TEMPLATE.replace("{slot_predicate}", slot_predicate)


def count_orgs_with_corpus(predicate: str) -> str:
    """0007's function with the same swap, and public for the same reason."""
    return _COUNT_TEMPLATE.replace("{predicate}", predicate)


def copy_catalogue_corpus_into() -> str:
    """The copy function, with the template organisation's id substituted.

    A marker rather than an f-string, for 0011's reason: an f-string trips
    ruff's S608 and the honest answer is to stop interpolating rather than to
    silence the rule. The value is a module constant, never input.
    """
    return _COPY_CATALOGUE_CORPUS_TEMPLATE.replace("{public_corpus_org}", _PUBLIC_CORPUS_ORG)


def upgrade() -> None:
    op.execute(copy_catalogue_corpus_into())
    # New functions are executable by PUBLIC by default. Revoke before granting,
    # or the explicit grant is decoration — 0007's note, and it holds for every
    # function this repository adds.
    op.execute("REVOKE ALL ON FUNCTION public.copy_catalogue_corpus_into(uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION public.copy_catalogue_corpus_into(uuid) TO lexloom_app")

    # CREATE OR REPLACE with unchanged signatures, so existing grants survive.
    op.execute(count_orgs_with_corpus(_COUNT_PREDICATE_V2))
    op.execute(reserve_ingest_slot(_SLOT_PREDICATE_V2))


def downgrade() -> None:
    op.execute(reserve_ingest_slot(_SLOT_PREDICATE_V1))
    op.execute(count_orgs_with_corpus(_COUNT_PREDICATE_V1))
    op.execute("DROP FUNCTION IF EXISTS public.copy_catalogue_corpus_into(uuid)")
