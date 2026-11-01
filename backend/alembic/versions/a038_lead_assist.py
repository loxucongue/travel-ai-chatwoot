"""Keep lead notifications separate from exclusive human ownership."""
from alembic import op
import sqlalchemy as sa

revision = 'a038_lead_assist'
down_revision = 'a037_bi_reporting'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('handoff_tasks', sa.Column('ai_assist_allowed', sa.Boolean(), nullable=False, server_default='0'))


def downgrade():
    op.drop_column('handoff_tasks', 'ai_assist_allowed')
