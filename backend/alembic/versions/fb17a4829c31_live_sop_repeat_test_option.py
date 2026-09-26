"""Add an explicit allowlisted repeat-delivery option for live SOP tests."""
from alembic import op
import sqlalchemy as sa

revision = "fb17a4829c31"
down_revision = "fa12c8d4310b"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "live_sop_enrollments",
        sa.Column("allow_repeat_delivery", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade():
    op.drop_column("live_sop_enrollments", "allow_repeat_delivery")
