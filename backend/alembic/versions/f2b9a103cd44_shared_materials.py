"""Shared material rehearsal ledger and route-bound SOPs."""
from alembic import op
import sqlalchemy as sa

revision = "f2b9a103cd44"
down_revision = "e9b1c307aa62"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sop_definitions", sa.Column("route_variant", sa.String(60), nullable=False, server_default=""))
    op.add_column("sop_definitions", sa.Column("test_conversation_ids", sa.JSON(), nullable=False, server_default="[]"))
    op.create_table("material_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("claim_key", sa.String(64), nullable=False, unique=True),
        sa.Column("subject_key", sa.String(160), nullable=False),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("automation_sessions.id"), nullable=False),
        sa.Column("business_key", sa.String(160), nullable=False),
        sa.Column("content_family", sa.String(160), nullable=False),
        sa.Column("asset_hash", sa.String(64), nullable=False),
        sa.Column("media_id", sa.Integer(), sa.ForeignKey("stored_media.id"), nullable=False),
        sa.Column("source", sa.String(30), nullable=False),
        sa.Column("route_variant", sa.String(60), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("confirmed_at", sa.String(40), nullable=False))
    op.create_index("ix_material_deliveries_subject_key", "material_deliveries", ["subject_key"])


def downgrade():
    raise RuntimeError("Restore a backup to preserve material delivery history")
