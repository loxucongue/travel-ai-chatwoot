"""Add isolated live SOP test enrollments and jobs."""
from alembic import op
import sqlalchemy as sa

revision = "fa12c8d4310b"
down_revision = "f4a2c518ef76"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "live_sop_enrollments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sop_id", sa.Integer(), sa.ForeignKey("sop_definitions.id"), nullable=False),
        sa.Column("sop_version_id", sa.Integer(), sa.ForeignKey("sop_versions.id"), nullable=False),
        sa.Column("conversation_state_id", sa.Integer(), sa.ForeignKey("conversation_states.id"), nullable=False),
        sa.Column("subject_key", sa.String(160), nullable=False),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("request_key", sa.String(80), nullable=False),
        sa.Column("trigger_source", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("enrolled_at", sa.String(40), nullable=False),
        sa.Column("expires_at", sa.String(40), nullable=False),
        sa.Column("completed_at", sa.String(40)),
        sa.Column("exit_reason", sa.String(120)),
        sa.UniqueConstraint("sop_id", "subject_key", "round_number", name="uq_live_sop_subject_round"),
        sa.UniqueConstraint("sop_id", "subject_key", "request_key", name="uq_live_sop_subject_request"),
    )
    op.create_index("ix_live_sop_enrollments_status", "live_sop_enrollments", ["status"])
    op.create_index("uq_live_sop_active_subject", "live_sop_enrollments", ["sop_id", "subject_key"],
                    unique=True, sqlite_where=sa.text("status = 'active'"))
    op.create_table(
        "live_sop_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enrollment_id", sa.Integer(), sa.ForeignKey("live_sop_enrollments.id"), nullable=False),
        sa.Column("node_key", sa.String(80), nullable=False),
        sa.Column("predecessor_id", sa.Integer(), sa.ForeignKey("live_sop_jobs.id")),
        sa.Column("scheduled_at", sa.String(40)),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("reason", sa.String(160)),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("confirmed_at", sa.String(40)),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("completed_at", sa.String(40)),
        sa.UniqueConstraint("enrollment_id", "node_key", name="uq_live_sop_enrollment_node"),
    )
    op.create_index("ix_live_sop_jobs_due", "live_sop_jobs", ["status", "scheduled_at"])


def downgrade():
    op.drop_table("live_sop_jobs")
    op.drop_table("live_sop_enrollments")
