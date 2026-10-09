"""Durable advisor routing and notification recipients."""
from alembic import op
import sqlalchemy as sa

revision = 'a033_advisor_assignment'
down_revision = 'a032_control_order'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('notifications', sa.Column('target_agent_id', sa.Integer(), nullable=True))
    op.create_table('advisor_rotations', sa.Column('key', sa.String(160), primary_key=True),
                    sa.Column('position', sa.Integer(), nullable=False))
    op.create_table('advisor_assignments',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('conversation_state_id', sa.Integer(), sa.ForeignKey('conversation_states.id'), nullable=False),
        sa.Column('event_key', sa.String(160), nullable=False), sa.Column('event_type', sa.String(60), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False), sa.Column('rule_id', sa.String(80)),
        sa.Column('rule_name', sa.String(120), nullable=False), sa.Column('action', sa.String(20), nullable=False),
        sa.Column('agent_id', sa.Integer()), sa.Column('status', sa.String(30), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False), sa.Column('available_at', sa.String(40), nullable=False),
        sa.Column('error', sa.String(200)), sa.Column('notification_id', sa.Integer(), sa.ForeignKey('notifications.id')),
        sa.Column('created_at', sa.String(40), nullable=False), sa.Column('updated_at', sa.String(40), nullable=False),
        sa.UniqueConstraint('conversation_state_id', 'event_key'))
    op.create_index('ix_assignment_pending','advisor_assignments',['status','available_at'])


def downgrade():
    op.drop_table('advisor_assignments')
    op.drop_table('advisor_rotations')
    op.drop_column('notifications','target_agent_id')
