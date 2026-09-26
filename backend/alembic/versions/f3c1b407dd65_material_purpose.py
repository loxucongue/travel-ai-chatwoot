"""Track route-scoped material purpose independently of file identity."""
from alembic import op
import sqlalchemy as sa

revision = "f3c1b407dd65"
down_revision = "f2b9a103cd44"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("material_deliveries",sa.Column("content_group_key",sa.String(160),nullable=False,server_default=""))
    op.create_index("ix_material_deliveries_content_group_key","material_deliveries",["content_group_key"])


def downgrade():
    op.drop_index("ix_material_deliveries_content_group_key",table_name="material_deliveries")
    op.drop_column("material_deliveries","content_group_key")
