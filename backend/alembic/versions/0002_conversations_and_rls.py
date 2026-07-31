"""conversations, messages, chunks and row-level security

Revision ID: 0002
"""

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0002"
down_revision = "0001"

ORG_SCOPED_TABLES = ("conversations", "messages", "document_chunks")


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(255), nullable=False, server_default="New conversation"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_conversations_org_created", "conversations", ["org_id", "created_at"])

    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("finish_reason", sa.String(32), nullable=True),
        sa.Column("citations", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_messages_org_conversation", "messages", ["org_id", "conversation_id"])

    op.create_table(
        "document_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("page_from", sa.Integer(), nullable=False),
        sa.Column("page_to", sa.Integer(), nullable=False),
        sa.Column("embedding", Vector(384), nullable=True),
    )
    op.create_index("ix_document_chunks_org", "document_chunks", ["org_id"])

    for table in ORG_SCOPED_TABLES:
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO lexloom_app, lexloom_worker"
        )
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # FORCE binds a non-superuser table owner. In this stack the migration
        # role is the Compose bootstrap superuser and bypasses RLS regardless
        # of FORCE — superuser bypass is unconditional and outranks it. FORCE
        # is what keeps the policy binding once that is fixed.
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY org_isolation ON {table}
              FOR ALL TO lexloom_app, lexloom_worker
              USING      (org_id = nullif(current_setting('app.current_org_id', true), '')::uuid)
              WITH CHECK (org_id = nullif(current_setting('app.current_org_id', true), '')::uuid)
            """
        )


def downgrade() -> None:
    for table in ORG_SCOPED_TABLES:
        op.execute(f"DROP POLICY IF EXISTS org_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.drop_table("document_chunks")
    op.drop_table("messages")
    op.drop_table("conversations")
