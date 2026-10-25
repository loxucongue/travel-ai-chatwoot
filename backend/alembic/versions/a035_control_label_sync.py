"""Persist Chatwoot control-label intents across failures and restarts."""
from alembic import op
import sqlalchemy as sa

revision = 'a035_control_label_sync'
down_revision = 'a034_customer_quotes'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('conversation_label_syncs',
        sa.Column('conversation_state_id',sa.Integer(),sa.ForeignKey('conversation_states.id'),primary_key=True),
        sa.Column('revision',sa.Integer(),nullable=False),
        sa.Column('target',sa.String(20),nullable=False),
        sa.Column('status',sa.String(20),nullable=False),
        sa.Column('attempts',sa.Integer(),nullable=False),
        sa.Column('due_at',sa.String(40),nullable=False),
        sa.Column('error',sa.String(200),nullable=True),
        sa.Column('updated_at',sa.String(40),nullable=False))


def downgrade():
    op.drop_table('conversation_label_syncs')
