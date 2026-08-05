"""a chunk may have no pages, and may say where it is in words

`page_from` / `page_to` were `NOT NULL` from 0002, when every chunk in this
system came from one paginated PDF. A `.docx` built from a web publication has
no pagination at all, and a chunk that invented a page number would be a number
pointing at nothing — which is the precise failure mode this repository's README
is a case study about. So the columns become nullable and `locator` carries the
position instead: for the `.docx`, the heading path
("Authentication Assurance Levels > ... > Reauthentication"), which a reader can
find; for the PDF, `NULL`, because pages already say it better.

**No backfill, and the reason is not the one three drafts of the plan gave.**
Those drafts argued a backfill was impossible because `document_chunks` carries
`FORCE ROW LEVEL SECURITY`. That is false here and 0002 says so in its own
comment: *"the migration role is the Compose bootstrap superuser and bypasses
RLS regardless of FORCE — superuser bypass is unconditional and outranks it."*
0006 declined its own backfill on cost ("Not worth it"), not on impossibility.

The real reason there is nothing to backfill is semantic: an existing row is a
PDF or seed chunk, and a PDF chunk has no heading path. `NULL` is not an
unfilled value here, it is the permanent right answer, and `parse.py` and
`corpus.py` both write the column going forward.

**`lock_timeout`.** Alembic wraps `upgrade()` in one transaction, so the
`ACCESS EXCLUSIVE` lock the first `ALTER TABLE` takes is held until the last
statement commits. Both statements here are catalogue-only and instant; the
hazard is *acquiring* the lock, which waits behind any open reader and queues
every later reader behind itself. On a shared box that turns one slow query into
a stall of chat and ingest alike. No migration in this repository set one before
this; three seconds is short enough that a busy moment fails the deploy instead
of freezing the service, and the operator retries.
"""

import sqlalchemy as sa

from alembic import op

revision = "0009"
down_revision = "0008"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.add_column("document_chunks", sa.Column("locator", sa.Text(), nullable=True))
    op.alter_column("document_chunks", "page_from", existing_type=sa.Integer(), nullable=True)
    op.alter_column("document_chunks", "page_to", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    """Unconditional, like all eight migrations before it.

    An earlier draft wanted this to raise once any page-less chunk existed. That
    would contradict `deploy/README.md`'s promise that every migration has a
    working `downgrade()`, and it would turn an emergency rollback into hand-run
    SQL on a shared production box at the worst possible moment.

    Deleting the page-less rows is safe for the same reason 0006 gave for not
    backfilling: they belong to demo tenants that expire within hours, and the
    corpus they came from is regenerable by re-running the ingest. Restoring
    `NOT NULL` without deleting them is impossible, and leaving the columns
    nullable would leave the schema in a state 0008 never described — a
    `downgrade()` that does not return the previous state is not one.
    """
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.execute("DELETE FROM document_chunks WHERE page_from IS NULL OR page_to IS NULL")
    op.alter_column("document_chunks", "page_to", existing_type=sa.Integer(), nullable=False)
    op.alter_column("document_chunks", "page_from", existing_type=sa.Integer(), nullable=False)
    op.drop_column("document_chunks", "locator")
