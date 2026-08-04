"""search_mode and generator columns on messages

Revision ID: 0005

Both columns land in one migration, even though the search-mode toggle
(`search_mode`) is Task 21's work in a sibling branch and the generator
selector (`generator`) is this task's. Two migrations against `messages` from
two worktrees would mean two `0005`s racing for the same `down_revision` — a
broken revision chain, not a mergeable one. This migration is the one place
both columns are added; Task 21 finds `search_mode` already there.

`NOT NULL` with a server-side default, not nullable: existing rows get an
honest value rather than `NULL`, which would later be read as "unknown" and
propagate into evals. `generator` defaults to `'deterministic'`, matching
`app.chat.providers.DEFAULT_GENERATOR`.

Catalogue inventory is unaffected: `EXPECTED_ROUTES` do not change (no new
route), and `EXPECTED_RELATIONS` is keyed on `(schema, name, relkind)` with no
column dimension, so `purge_expired_orgs`'s residual argument (migration 0004)
is untouched by this migration and still holds unchanged for the existing
routes.
"""

from alembic import op

revision = "0005"
down_revision = "0004"


def upgrade() -> None:
    op.execute(
        "ALTER TABLE messages "
        "ADD COLUMN search_mode varchar(16) NOT NULL DEFAULT 'fts', "
        "ADD COLUMN generator varchar(32) NOT NULL DEFAULT 'deterministic'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE messages DROP COLUMN generator, DROP COLUMN search_mode")
