"""Pin V1/V2 reception engines to sessions and durable work."""
from alembic import op
import sqlalchemy as sa

revision = "e3a91c04f7b2"
down_revision = "d2f6a8b901ce"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("conversation_states") as batch:
        batch.add_column(sa.Column("ai_engine_version", sa.String(20), nullable=False, server_default="v1"))
        batch.add_column(sa.Column("ai_engine_release_id", sa.String(100), nullable=False, server_default="v1"))
    with op.batch_alter_table("automation_sessions") as batch:
        batch.add_column(sa.Column("engine_version", sa.String(20), nullable=False, server_default="v1"))
        batch.add_column(sa.Column("engine_release_id", sa.String(100), nullable=False, server_default="v1"))
    with op.batch_alter_table("live_reply_jobs") as batch:
        batch.add_column(sa.Column("engine_version", sa.String(20), nullable=False, server_default="v1"))
        batch.add_column(sa.Column("engine_release_id", sa.String(100), nullable=False, server_default="v1"))
    with op.batch_alter_table("live_sop_enrollments") as batch:
        batch.add_column(sa.Column("engine_version", sa.String(20), nullable=False, server_default="v1"))
        batch.add_column(sa.Column("engine_release_id", sa.String(100), nullable=False, server_default="v1"))


def downgrade() -> None:
    with op.batch_alter_table("live_sop_enrollments") as batch:
        batch.drop_column("engine_release_id")
        batch.drop_column("engine_version")
    with op.batch_alter_table("live_reply_jobs") as batch:
        batch.drop_column("engine_release_id")
        batch.drop_column("engine_version")
    with op.batch_alter_table("automation_sessions") as batch:
        batch.drop_column("engine_release_id")
        batch.drop_column("engine_version")
    with op.batch_alter_table("conversation_states") as batch:
        batch.drop_column("ai_engine_release_id")
        batch.drop_column("ai_engine_version")
