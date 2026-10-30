"""Rebuildable customer events and freshness for the operations dashboard."""
from alembic import op
import sqlalchemy as sa

revision = 'a037_bi_reporting'
down_revision = 'a036_business_labels'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('bi_conversations',
        sa.Column('conversation_state_id',sa.Integer(),sa.ForeignKey('conversation_states.id'),primary_key=True),
        sa.Column('source_created_at',sa.String(40),nullable=True),
        sa.Column('signature',sa.String(64),nullable=False),
        sa.Column('snapshot',sa.JSON(),nullable=False),
        sa.Column('refreshed_at',sa.String(40),nullable=False))
    op.create_table('bi_events',
        sa.Column('key',sa.String(250),primary_key=True),
        sa.Column('conversation_state_id',sa.Integer(),sa.ForeignKey('conversation_states.id'),nullable=False),
        sa.Column('kind',sa.String(40),nullable=False),
        sa.Column('occurred_at',sa.String(40),nullable=False),
        sa.Column('source',sa.String(40),nullable=False),
        sa.Column('evidence',sa.JSON(),nullable=False))
    op.create_index('ix_bi_event_time_kind','bi_events',['occurred_at','kind'])
    op.create_index('ix_bi_event_conversation','bi_events',['conversation_state_id','occurred_at'])
    op.create_table('bi_refresh',sa.Column('id',sa.Integer(),primary_key=True),
        sa.Column('refreshed_at',sa.String(40),nullable=False),sa.Column('remaining',sa.Integer(),nullable=False))


def downgrade():
    op.drop_table('bi_events')
    op.drop_table('bi_conversations')
    op.drop_table('bi_refresh')
