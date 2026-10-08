"""Remember source revision for Chatwoot conversation controls."""
from alembic import op
import sqlalchemy as sa

revision = 'a032_control_order'
down_revision = 'a031v3_runtime'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('conversation_states', sa.Column('chatwoot_control_updated_at', sa.Float(), nullable=False, server_default='0'))


def downgrade():
    op.drop_column('conversation_states', 'chatwoot_control_updated_at')
