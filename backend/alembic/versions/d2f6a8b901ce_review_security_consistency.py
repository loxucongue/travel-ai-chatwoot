"""Per-user notification receipts and exclusive handoff assignments."""
from alembic import op
import sqlalchemy as sa

revision = "d2f6a8b901ce"
down_revision = "d1e7a9c4b205"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    duplicates = connection.execute(sa.text(
        "SELECT chatwoot_agent_id FROM users WHERE chatwoot_agent_id IS NOT NULL "
        "GROUP BY chatwoot_agent_id HAVING COUNT(*) > 1"
    )).scalars().all()
    if duplicates:
        # Do not silently detach a user's channel identity during deployment.
        raise RuntimeError(f"duplicate_chatwoot_agent_bindings: {duplicates}; resolve bindings before upgrading")
    constraints = sa.inspect(connection).get_unique_constraints("users")
    if not any(item["column_names"] == ["chatwoot_agent_id"] for item in constraints):
        with op.batch_alter_table("users") as batch:
            batch.create_unique_constraint("uq_users_chatwoot_agent_id", ["chatwoot_agent_id"])
    with op.batch_alter_table("handoff_tasks") as batch:
        batch.add_column(sa.Column("assignment_target_user_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("assignment_target_agent_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_handoff_assignment_target", "users", ["assignment_target_user_id"], ["id"])
    op.create_table(
        "notification_reads",
        sa.Column("notification_id", sa.Integer(), sa.ForeignKey("notifications.id"), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("read_at", sa.String(40), nullable=False),
    )
    # Only personal receipts have an identifiable reader. Broadcasts become
    # unread per user rather than guessing who read the old shared record.
    connection.execute(sa.text(
        "INSERT INTO notification_reads (notification_id, user_id, read_at) "
        "SELECT id, user_id, read_at FROM notifications WHERE user_id IS NOT NULL AND read_at IS NOT NULL"
    ))


def downgrade() -> None:
    op.drop_table("notification_reads")
    with op.batch_alter_table("handoff_tasks") as batch:
        batch.drop_constraint("fk_handoff_assignment_target", type_="foreignkey")
        batch.drop_column("assignment_target_user_id")
        batch.drop_column("assignment_target_agent_id")
    if any(item["name"] == "uq_users_chatwoot_agent_id" for item in sa.inspect(op.get_bind()).get_unique_constraints("users")):
        with op.batch_alter_table("users") as batch:
            batch.drop_constraint("uq_users_chatwoot_agent_id", type_="unique")
