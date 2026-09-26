"""add explicit conversation AI mode and Chatwoot label sync state

Revision ID: c4f7a8b2d901
Revises: b77d1a4c203e
"""
from alembic import op
import sqlalchemy as sa


revision = "c4f7a8b2d901"
down_revision = "b77d1a4c203e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("conversation_states") as batch:
        batch.add_column(sa.Column("ai_mode", sa.String(length=20), nullable=False, server_default="inherit"))
        batch.add_column(sa.Column("ai_mode_source", sa.String(length=30), nullable=False, server_default="system"))
        batch.add_column(sa.Column("ai_label_present", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("ai_sync_status", sa.String(length=20), nullable=False, server_default="synced"))
        batch.add_column(sa.Column("ai_mode_updated_at", sa.String(length=40), nullable=True))
    op.execute("UPDATE conversation_states SET ai_mode = 'enabled', ai_label_present = 1, ai_mode_source = 'migration' WHERE EXISTS (SELECT 1 FROM json_each(conversation_states.labels) WHERE lower(json_each.value) = 'ai')")


def downgrade() -> None:
    with op.batch_alter_table("conversation_states") as batch:
        batch.drop_column("ai_mode_updated_at")
        batch.drop_column("ai_sync_status")
        batch.drop_column("ai_label_present")
        batch.drop_column("ai_mode_source")
        batch.drop_column("ai_mode")
