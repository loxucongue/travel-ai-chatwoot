"""add web knowledge source and revision tables

Revision ID: b72f6b9a130c
Revises: a91d4e7c2b65
"""
from alembic import op
import sqlalchemy as sa


revision = "b72f6b9a130c"
down_revision = "a91d4e7c2b65"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "web_knowledge_sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("url", sa.String(1200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("match_keywords", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="active"),
        sa.Column("ai_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sync_status", sa.String(30), nullable=False, server_default="never"),
        sa.Column("latest_revision_id", sa.Integer(), nullable=True),
        sa.Column("published_revision_id", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_checked_at", sa.String(40), nullable=True),
        sa.Column("last_changed_at", sa.String(40), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.UniqueConstraint("tenant_id", "url"),
    )
    op.create_index("ix_web_knowledge_sources_tenant_status", "web_knowledge_sources", ["tenant_id", "status"])
    op.create_table(
        "web_knowledge_revisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("web_knowledge_sources.id"), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(500), nullable=False, server_default=""),
        sa.Column("final_url", sa.String(1200), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("content_length", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending_review"),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("fetched_at", sa.String(40), nullable=False),
        sa.Column("published_at", sa.String(40), nullable=True),
        sa.UniqueConstraint("source_id", "revision_number"),
        sa.UniqueConstraint("source_id", "content_hash"),
    )
    op.create_index("ix_web_knowledge_revisions_source", "web_knowledge_revisions", ["source_id", "revision_number"])


def downgrade() -> None:
    op.drop_index("ix_web_knowledge_revisions_source", table_name="web_knowledge_revisions")
    op.drop_table("web_knowledge_revisions")
    op.drop_index("ix_web_knowledge_sources_tenant_status", table_name="web_knowledge_sources")
    op.drop_table("web_knowledge_sources")
