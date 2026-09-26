"""Add opt-in live reply jobs without changing historical records."""
from alembic import op
import sqlalchemy as sa

revision = "f4a2c518ef76"
down_revision = "f3c1b407dd65"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("live_reply_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("conversation_state_id", sa.Integer(), sa.ForeignKey("conversation_states.id"), nullable=False),
        sa.Column("trigger_message_id", sa.Integer(), sa.ForeignKey("message_events.id"), nullable=False, unique=True),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("input_ids", sa.JSON(), nullable=False),
        sa.Column("decision", sa.JSON(), nullable=False),
        sa.Column("trace", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(160)),
        sa.Column("due_at", sa.String(40), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("completed_at", sa.String(40)))
    for key in ("conversation_state_id", "status", "due_at"):
        op.create_index(f"ix_live_reply_jobs_{key}", "live_reply_jobs", [key])


def downgrade():
    op.drop_table("live_reply_jobs")
