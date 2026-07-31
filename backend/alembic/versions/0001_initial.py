"""initial schema: extensions and organizations

Revision ID: 0001
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "organizations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("external_id", sa.String(255), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_organizations_expires_at", "organizations", ["expires_at"])
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON organizations TO lexloom_app, lexloom_worker"
    )


def downgrade() -> None:
    op.drop_index("ix_organizations_expires_at", table_name="organizations")
    op.drop_table("organizations")
    op.execute("DROP EXTENSION IF EXISTS vector")
