"""add protected web source authentication and extraction metadata

Revision ID: c8a4d2e719bf
Revises: d8e4f2a619ab, b72f6b9a130c
"""
from alembic import op
import sqlalchemy as sa


revision = "c8a4d2e719bf"
down_revision = ("d8e4f2a619ab", "b72f6b9a130c")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("web_knowledge_sources", sa.Column("auth_type", sa.String(40), nullable=False, server_default="none"))
    op.add_column("web_knowledge_sources", sa.Column("auth_secret", sa.LargeBinary(), nullable=True))
    op.add_column("web_knowledge_revisions", sa.Column("script_blocks", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("web_knowledge_revisions", sa.Column("image_candidates", sa.JSON(), nullable=False, server_default="[]"))


def downgrade() -> None:
    op.drop_column("web_knowledge_revisions", "image_candidates")
    op.drop_column("web_knowledge_revisions", "script_blocks")
    op.drop_column("web_knowledge_sources", "auth_secret")
    op.drop_column("web_knowledge_sources", "auth_type")
