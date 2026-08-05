"""The schema the migrations produce, and the schema the models describe.

Two tests with very different costs.

The first is read-only and always runs: it compares `document_chunks` as the
database actually has it against `app/db/models.py`. That pairing matters
because `alembic/env.py` points `target_metadata` at that metadata, so a model
that drifts from a migration makes the next `--autogenerate` emit a migration
that *reverts* the one before it — a failure that is silent until someone
generates a migration months later and does not read it closely.

The second round-trips a migration, which means running `downgrade`, which for
0009 **deletes rows**. It therefore refuses to run against the database the rest
of the suite uses, and refuses again unless it is pointed at one it is allowed
to destroy. The shared development Postgres also serves a live deployment; a
test that quietly dropped a column there would be a worse bug than the one it
was written to catch.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from app.db.session import system_session

pytestmark = pytest.mark.asyncio

COLUMNS_SQL = text(
    """
    SELECT column_name, is_nullable, data_type
      FROM information_schema.columns
     WHERE table_name = 'document_chunks'
     ORDER BY column_name
    """
)


async def _columns() -> dict[str, tuple[str, str]]:
    async with system_session() as session:
        rows = (await session.execute(COLUMNS_SQL)).all()
    return {name: (nullable, dtype) for name, nullable, dtype in rows}


async def test_the_live_schema_is_the_one_the_models_describe() -> None:
    """`page_from`/`page_to` nullable and `locator` present — migration 0009.

    Asserted against the database rather than against the migration file,
    because what breaks a deployment is the gap between them, not the file.
    """
    columns = await _columns()

    assert "locator" in columns, "migration 0009 has not been applied to this database"
    assert columns["locator"][0] == "YES"
    assert columns["page_from"][0] == "YES"
    assert columns["page_to"][0] == "YES"

    from app.db.models import DocumentChunk

    for name in ("page_from", "page_to", "locator"):
        column = DocumentChunk.__table__.c[name]
        assert column.nullable is True, (
            f"models.py says {name} is NOT NULL but the database says otherwise; "
            "the next --autogenerate will emit a migration reverting 0009"
        )


@pytest.mark.skipif(
    not os.environ.get("MIGRATION_TEST_DATABASE_URL"),
    reason=(
        "round-tripping 0009 runs its downgrade, which DELETEs every page-less "
        "chunk. Set MIGRATION_TEST_DATABASE_URL to a database you are willing to "
        "lose — never to the shared development one, which also serves a live "
        "deployment."
    ),
)
async def test_migration_0009_round_trips_to_the_same_schema() -> None:
    """`upgrade head` -> `downgrade -1` -> `upgrade head`, comparing the schema.

    The comparison is the point. A round-trip that only checks the commands
    succeed passes just as well for a `downgrade()` that leaves the columns
    nullable — which is precisely the shortcut an earlier draft of this
    migration proposed, and which would leave the schema in a state no revision
    ever described.
    """
    from alembic.config import Config

    from alembic import command

    url = os.environ["MIGRATION_TEST_DATABASE_URL"]
    assert url != os.environ.get("DATABASE_URL"), (
        "MIGRATION_TEST_DATABASE_URL points at the suite's own database"
    )

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)

    def snapshot() -> dict[str, tuple[str, str]]:
        from sqlalchemy import create_engine

        engine = create_engine(url.replace("+asyncpg", "+psycopg2"))
        try:
            with engine.connect() as conn:
                return {
                    name: (nullable, dtype)
                    for name, nullable, dtype in conn.execute(COLUMNS_SQL).all()
                }
        finally:
            engine.dispose()

    command.upgrade(config, "head")
    before = snapshot()
    command.downgrade(config, "-1")
    after_down = snapshot()
    command.upgrade(config, "head")
    after_up = snapshot()

    assert "locator" not in after_down
    assert after_down["page_from"][0] == "NO", "downgrade left page_from nullable"
    assert after_down["page_to"][0] == "NO", "downgrade left page_to nullable"
    assert after_up == before
