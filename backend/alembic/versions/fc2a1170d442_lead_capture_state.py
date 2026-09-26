"""Persist one-time contact requests and captured lead state."""
from alembic import op
import sqlalchemy as sa


revision = "fc2a1170d442"
down_revision = "fb17a4829c31"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "lead_capture_states",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conversation_state_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("requested_at", sa.String(length=40), nullable=True),
        sa.Column("captured_at", sa.String(length=40), nullable=True),
        sa.Column("captured_kinds", sa.JSON(), nullable=False),
        sa.Column("masked_values", sa.JSON(), nullable=False),
        sa.Column("source_message_id", sa.Integer(), nullable=True),
        sa.Column("label_sync_status", sa.String(length=30), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(["conversation_state_id"], ["conversation_states.id"]),
        sa.ForeignKeyConstraint(["source_message_id"], ["message_events.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_state_id"),
    )
    op.create_index(
        "ix_lead_capture_status_updated",
        "lead_capture_states",
        ["status", "updated_at"],
        unique=False,
    )


def downgrade():
    op.drop_index("ix_lead_capture_status_updated", table_name="lead_capture_states")
    op.drop_table("lead_capture_states")
