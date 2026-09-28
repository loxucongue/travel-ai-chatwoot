"""V3 session concurrency and exclusive live session binding."""
from alembic import op
import sqlalchemy as sa

revision = 'a031v3_runtime'
down_revision = 'e3a91c04f7b2'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('automation_sessions', sa.Column('revision', sa.Integer(), nullable=False, server_default='1'))
    op.create_index('uq_v3_live_conversation', 'automation_sessions', ['conversation_state_id'], unique=True,
                    sqlite_where=sa.text("environment = 'live'"))
    op.execute("UPDATE live_reply_jobs SET status='cancelled', error_code='engine_retired' WHERE status IN ('queued','processing')")
    op.execute("UPDATE live_sop_jobs SET status='cancelled', reason='engine_retired' WHERE status IN ('scheduled','waiting_dependency','processing')")
    op.execute("UPDATE live_sop_enrollments SET status='stopped', exit_reason='engine_retired' WHERE status='active'")
    op.execute("UPDATE conversation_states SET ai_engine_version='v3', ai_engine_release_id='v3', version=version+1")
    op.execute("UPDATE automation_sessions SET due_at=NULL WHERE engine_version<>'v3'")
    op.execute("UPDATE automation_runs SET status='cancelled', error_code='engine_retired' WHERE status IN ('pending','processing') AND session_id IN (SELECT id FROM automation_sessions WHERE engine_version<>'v3')")


def downgrade():
    op.drop_index('uq_v3_live_conversation', table_name='automation_sessions')
    op.drop_column('automation_sessions', 'revision')
