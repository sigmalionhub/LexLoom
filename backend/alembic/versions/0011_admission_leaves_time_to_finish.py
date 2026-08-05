"""refuse an ingest the organisation will not outlive, rather than start it

`reserve_ingest_slot` shortens an ingesting tenant's life with
`LEAST(expires_at, now() + p_org_ttl_seconds)` — never lengthening it. That is
0008's defence against one address parking the global cap for a day, and
`test_admission_never_extends_an_organisations_ttl` holds it to that.

The rule was complete while a tenant ingested once. It is not once a tenant can
ingest a second document: the first ingest already pinned `expires_at` to
`now() + 7200`, so a second admission at T+1 h 55 m leaves five minutes of
organisation life against a `task_time_limit` of 360 s. The job is then killed
mid-flight by the purge rather than by anything about itself — `_ALIVE_SQL`
finds no row, `IngestAlreadyComplete`'s sibling `IngestAbandoned` is raised, and
the tenant sees a failure it did nothing to cause, after paying for a full parse
and embed.

A first draft of this migration fixed that by flooring the new expiry at
`now() + 600 s`. It worked and it was wrong: it extends a tenant's life, which
is precisely the property 0008 built and an existing test defends. Extending is
also the weaker fix — it spends the defence to buy a job that was admitted on a
guess.

So this refuses instead. An organisation with less life left than the task is
allowed to take is not admitted at all, and nothing is extended. The visitor
gets an immediate 503 rather than a failure five minutes into an ingest, and a
fresh login gives them a fresh organisation with a full day.

600 s rather than 360 s: the task's own limit is the floor of what it needs, and
a floor with no margin is one queue hiccup away from the failure it prevents.
"""

from alembic import op

revision = "0011"
down_revision = "0010"


# Seconds of organisation life an admission requires. `task_time_limit` is 360
# (app/worker/celery_app.py); this is that plus a margin, because a threshold
# with none is one queue hiccup away from the failure it exists to prevent.
_REQUIRED_LIFE_SECONDS = 600

_LIVENESS_WITH_MARGIN = (
    "       AND (o.expires_at IS NULL\n"
    # The right-hand side is parenthesised deliberately. `OPERATOR(...)` binds
    # more tightly than a bare operator, so without the brackets Postgres reads
    # `(expires_at > now()) + interval` and fails with "operator does not exist:
    # boolean + interval" — which is how this was found.
    "            OR o.expires_at OPERATOR(pg_catalog.>) (pg_catalog.now()\n"
    "               OPERATOR(pg_catalog.+) pg_catalog.make_interval(secs => "
    f"{_REQUIRED_LIFE_SECONDS})));"
)

_LIVENESS_BARE = """       AND (o.expires_at IS NULL
            OR o.expires_at OPERATOR(pg_catalog.>) pg_catalog.now());"""

_TEMPLATE = """
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
{liveness_clause}

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
                  AND c.source_key OPERATOR(pg_catalog.<>) 'seed:gdpr-excerpts'
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
                            AND c.source_key OPERATOR(pg_catalog.<>) 'seed:gdpr-excerpts'
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


def _reserve_ingest_slot(liveness_clause: str) -> str:
    """0010's function with one expression swapped.

    Written as a substitution rather than as two full copies so the two versions
    cannot drift in the parts this migration is not about — the counting, the
    advisory lock and the slot predicate all stay exactly as 0010 left them, and
    a reader can see at a glance that they do.
    """
    # A template with a marker rather than an f-string: an f-string here trips
    # ruff's S608 (SQL built by interpolation), and the honest way to answer
    # that is to stop interpolating rather than to silence it. There is no
    # input either way — `expiry_expression` is one of the two literals above.
    return _TEMPLATE.replace("{liveness_clause}", liveness_clause)


def upgrade() -> None:
    op.execute(_reserve_ingest_slot(_LIVENESS_WITH_MARGIN))


def downgrade() -> None:
    op.execute(_reserve_ingest_slot(_LIVENESS_BARE))
