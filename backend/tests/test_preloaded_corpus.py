"""Every tenant is born holding the whole GDPR, and the ceiling still works.

Two claims, and the second is the one that would have broken quietly. The copy
is easy to see: a provisioned tenant either has 978 chunks of `gdpr-full` or it
does not. The ingest cap is not — `count_orgs_with_corpus` and
`reserve_ingest_slot` both asked "does this organisation hold a corpus" as
"holds anything that is not the seed", and provisioning now hands out something
that is not the seed. Left alone, `INGEST_ORG_CAP` would have filled with eight
ordinary visitors who had ingested nothing, and the second document would have
become un-ingestable for everybody, with no error anywhere.
"""

from __future__ import annotations

import importlib.util
import pathlib
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.auth.demo import provision_demo_org
from app.chat.corpus import SEED_SOURCE_KEY, load_excerpts
from app.db.session import tenant_session
from app.ingest.catalogue import CATALOGUE
from app.ingest.jobs import read_ingest_status
from app.ingest.preload import PRELOADED_SOURCE_KEYS

PRELOADED = "gdpr-full"


async def _provision(created_orgs: list[str]) -> str:
    org = await provision_demo_org("Preload test")
    created_orgs.append(org.id)
    return org.id


async def _counts(org_id: str) -> dict[str, int]:
    async with tenant_session(org_id) as session:
        rows = await session.execute(
            text("SELECT source_key, count(*) FROM document_chunks GROUP BY source_key")
        )
        return {key: count for key, count in rows}


async def test_a_provisioned_tenant_holds_the_whole_document_not_an_extract(
    created_orgs,
) -> None:
    org_id = await _provision(created_orgs)
    counts = await _counts(org_id)

    assert counts[PRELOADED] == CATALOGUE[PRELOADED].expected_chunks
    # The excerpts stay. They are not made redundant by the full text: the M2.4
    # gold set names `seed:gdpr-excerpts` chunks in its own judgements, and
    # `app/evals/corpus.py` measures against seed-plus-catalogue precisely
    # because that is what a live tenant holds. It now really is.
    assert counts[SEED_SOURCE_KEY] == len(load_excerpts())


async def test_the_preloaded_document_reports_itself_complete_without_an_ingest(
    created_orgs,
) -> None:
    """The card reads its state from the rows, and nobody wrote a progress hash.

    `_rows_fallback_status` is what makes this true, and it is worth a test
    rather than a comment: if it ever answered from Redis alone, a preloaded
    tenant would show the document it holds as "Not ingested" and offer a button
    that answers 409 — the contradiction this whole change exists to remove.
    """
    org_id = await _provision(created_orgs)
    status = await read_ingest_status(org_id, PRELOADED)

    assert status["stage"] == "complete"
    assert status["chunks_done"] == CATALOGUE[PRELOADED].expected_chunks


async def test_a_freshly_provisioned_tenant_does_not_occupy_an_ingest_slot(
    created_orgs,
) -> None:
    """The regression migration 0012 exists to prevent.

    Measured as a *delta*, not against a fixed cap. The development database is
    long-lived and shared, so some number of organisations already hold an
    ingested corpus at any moment; a test written against `cap=1` measures that
    ambient number and not this change. The claim here is exact and independent
    of it: provisioning a tenant must not move the counter, and the new tenant
    must still be admitted into the last free slot — which it could not be if
    the counter had already spent one on it.
    """
    async with tenant_session(str(uuid.uuid4())) as session:
        before = await session.scalar(text("SELECT count_orgs_with_corpus()"))

    org_id = await _provision(created_orgs)

    async with tenant_session(org_id) as session:
        after = await session.scalar(text("SELECT count_orgs_with_corpus()"))
        admitted = await session.scalar(
            text("SELECT reserve_ingest_slot(:cap, :res, :org)"),
            {"cap": after + 1, "res": 7200, "org": 7200},
        )

    assert after == before
    assert admitted is True


async def test_the_copy_function_refuses_to_fill_another_tenant(created_orgs) -> None:
    """`row_security = off` gives the function the power to write anywhere.

    The context check is the only thing that takes it away again, so it is
    asserted rather than trusted. Without it, any caller could seed rows into
    an organisation it does not own — a write primitive across the tenant
    boundary, inside the one table this repository exists to isolate.
    """
    org_id = await _provision(created_orgs)
    stranger = str(uuid.uuid4())

    with pytest.raises(DBAPIError, match="own tenant"):
        async with tenant_session(org_id) as session:
            await session.scalar(
                text("SELECT copy_catalogue_corpus_into(:org)"), {"org": stranger}
            )


def test_the_preloaded_keys_are_spelled_the_same_everywhere() -> None:
    """Three spellings of one decision, and nothing but this holds them together.

    `PRELOADED_SOURCE_KEYS` decides what the loader writes into the template;
    migration 0012's predicate decides what the ingest cap ignores. If they ever
    disagree, the cap silently counts a document every tenant was given — which
    is the exact bug 0012 was written to fix, reintroduced by an edit in the
    other file.
    """
    migration = _load_migration()
    for key in PRELOADED_SOURCE_KEYS:
        assert key in CATALOGUE
        assert f"'{key}'" in migration._SLOT_PREDICATE_V2
        assert f"'{key}'" in migration._COUNT_PREDICATE_V2
    assert f"'{SEED_SOURCE_KEY}'" in migration._SLOT_PREDICATE_V2
    assert f"'{SEED_SOURCE_KEY}'" in migration._COUNT_PREDICATE_V2


def test_migration_0012_downgrade_restores_0011_exactly() -> None:
    """0012 carries a copy of 0011's function body, so the copy is compared.

    The first draft of 0012 retyped that body from memory and dropped the
    `LEAST(...)` that floors `expires_at` — which is the entire subject of 0011.
    A downgrade that quietly removes a ceiling is worse than one that fails.
    """
    m11 = _load_migration("0011_admission_leaves_time_to_finish")
    m12 = _load_migration()

    assert m12.reserve_ingest_slot(m12._SLOT_PREDICATE_V1) == m11._reserve_ingest_slot(
        m11._LIVENESS_WITH_MARGIN
    )


def test_migration_0012_downgrade_restores_0008s_counter_not_0007s() -> None:
    """The same trap, sprung a second time in the same migration.

    `count_orgs_with_corpus` is *created* by 0007 and *replaced* by 0008, which
    re-points it at `organizations` so an orphaned tenant's chunks stop holding
    a slot forever. Rebuilding it from the file named after it reverts that, and
    the only thing that noticed was one test about orphans. The newest
    definition of a replaced function is never in the file it is named after.
    """
    m08 = _load_migration("0008_reserve_ingest_slot")
    m12 = _load_migration()

    assert m12.count_orgs_with_corpus(m12._COUNT_PREDICATE_V1) == m08.COUNT_ORGS_WITH_CORPUS_V2


def _load_migration(name: str = "0012_preload_catalogue_corpus"):
    """Import a revision module by path.

    Alembic's version files are not importable as `app.` modules — their names
    start with a digit — so the test loads them the way Alembic itself does.
    """
    path = pathlib.Path(__file__).resolve().parents[1] / "alembic" / "versions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
