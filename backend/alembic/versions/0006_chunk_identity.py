"""chunk identity: source_key and chunk_ordinal on document_chunks

Revision ID: 0006

`document_chunks.id` is a UUID, freshly generated and different in every
tenant's copy of the corpus, so an evaluation gold set cannot name a chunk by
it and mean the same chunk twice. The pair (`source_key`, `chunk_ordinal`) is
deterministic and identical across every tenant's copy: `source_key` names
which catalogue document (or the seed corpus) the chunk came from, and
`chunk_ordinal` is the chunk's position within that document's parse, in
`app.ingest.parse.parse_source`'s output order — or, for the seed corpus, its
position in `app.chat.corpus.load_excerpts()`.

Nullable, and not because `NOT NULL` was forgotten. `NOT NULL` needs a value
for every row already in the table, and those rows are per-tenant copies of
the seed corpus whose ordinal is per row — one server default would stamp
every one of them ordinal 0 and destroy the only property the pair exists to
give. Backfilling per row would mean iterating every live tenant from the
migration role under FORCE row-level security. Not worth it: a demo tenant's
rows are gone within 24 h under the TTL Task 32 enforces, and
`seed_public_corpus()` rewrites the permanent copy on the next deployment —
both paths write the new columns going forward (`app/chat/corpus.py`).

No index on the pair. ~600 rows per tenant, already reached through
`ix_document_chunks_org`; a sequential scan inside one tenant's rows costs a
fraction of a millisecond — the same arithmetic Task 20 used to skip an HNSW
index on `embedding`. Adding one here would be a new relation, which would
break `EXPECTED_RELATIONS` (`test_demo_orgs.py:166`) and belongs in its own
decision, not folded silently into this migration.
"""

import sqlalchemy as sa

from alembic import op

revision = "0006"
down_revision = "0005"


def upgrade() -> None:
    op.add_column("document_chunks", sa.Column("source_key", sa.String(64), nullable=True))
    op.add_column("document_chunks", sa.Column("chunk_ordinal", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("document_chunks", "chunk_ordinal")
    op.drop_column("document_chunks", "source_key")
