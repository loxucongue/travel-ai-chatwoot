"""Add environment scope for reviewed website knowledge."""

from alembic import op
import sqlalchemy as sa


revision = "d1e7a9c4b205"
down_revision = "c8a4d2e719bf"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "web_knowledge_sources",
        sa.Column("runtime_scope", sa.String(length=30), nullable=False, server_default="disabled"),
    )


def downgrade() -> None:
    op.drop_column("web_knowledge_sources", "runtime_scope")
