"""Persist route reply progress for the two live China2Go routes."""
from alembic import op
import sqlalchemy as sa


revision = "fd31c8a6b204"
down_revision = "fc2a1170d442"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "conversation_journeys",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conversation_state_id", sa.Integer(), nullable=False),
        sa.Column("knowledge_version_key", sa.String(length=100), nullable=False),
        sa.Column("route_variant", sa.String(length=80), nullable=False),
        sa.Column("stage", sa.String(length=50), nullable=False),
        sa.Column("slots", sa.JSON(), nullable=False),
        sa.Column("sent_groups", sa.JSON(), nullable=False),
        sa.Column("last_group_key", sa.String(length=100), nullable=True),
        sa.Column("last_trigger_message_id", sa.Integer(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(["conversation_state_id"], ["conversation_states.id"]),
        sa.ForeignKeyConstraint(["last_trigger_message_id"], ["message_events.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_state_id"),
    )


def downgrade():
    op.drop_table("conversation_journeys")
