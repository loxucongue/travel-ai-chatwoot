"""Durable additive business labels, without changing AI control state."""
from alembic import op
import sqlalchemy as sa

revision = 'a036_business_labels'
down_revision = 'a035_control_label_sync'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('conversation_label_additions',
        sa.Column('conversation_state_id', sa.Integer(), sa.ForeignKey('conversation_states.id'), primary_key=True),
        sa.Column('label', sa.String(100), primary_key=True),
        sa.Column('source_message_id', sa.Integer(), sa.ForeignKey('message_events.id'), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('due_at', sa.String(40), nullable=False),
        sa.Column('error', sa.String(200), nullable=True),
        sa.Column('updated_at', sa.String(40), nullable=False))


def downgrade():
    op.drop_table('conversation_label_additions')
