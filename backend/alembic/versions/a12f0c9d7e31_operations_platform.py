"""operations platform tables

Revision ID: a12f0c9d7e31
Revises: 6bf4f33f2eee
"""
from alembic import op
import sqlalchemy as sa

revision = "a12f0c9d7e31"
down_revision = "6bf4f33f2eee"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("analytics_cutover_at", sa.String(40), nullable=True))
    op.add_column("users", sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("users", sa.Column("chatwoot_agent_id", sa.Integer(), nullable=True))
    op.add_column("contacts", sa.Column("email", sa.String(320), nullable=True))
    op.add_column("contacts", sa.Column("phone_number", sa.String(80), nullable=True))
    op.add_column("contacts", sa.Column("custom_attributes", sa.JSON(), nullable=False, server_default="{}"))
    for name, column in [
        ("assignee_id", sa.Integer()), ("assignee_name", sa.String(160)),
        ("team_id", sa.Integer()), ("team_name", sa.String(160)),
    ]:
        op.add_column("conversation_states", sa.Column(name, column, nullable=True))
    op.add_column("message_events", sa.Column("sender_id", sa.Integer(), nullable=True))
    op.add_column("message_events", sa.Column("attribution", sa.String(40), nullable=False, server_default="unknown"))
    op.add_column("message_events", sa.Column("attachments", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("outbound_messages", sa.Column("source_type", sa.String(30), nullable=False, server_default="ai"))
    op.add_column("outbound_messages", sa.Column("source_id", sa.Integer(), nullable=True))
    op.add_column("outbound_messages", sa.Column("content_type", sa.String(30), nullable=False, server_default="text"))
    op.add_column("outbound_messages", sa.Column("media_id", sa.Integer(), nullable=True))

    op.create_table("chatwoot_agents",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("chatwoot_agent_id", sa.Integer(), nullable=False), sa.Column("name", sa.String(160), nullable=False),
        sa.Column("email", sa.String(320)), sa.Column("availability_status", sa.String(30), nullable=False),
        sa.Column("role", sa.String(40), nullable=False), sa.Column("inbox_ids", sa.JSON(), nullable=False), sa.Column("last_synced_at", sa.String(40), nullable=False),
        sa.UniqueConstraint("tenant_id", "chatwoot_agent_id"))
    op.create_table("chatwoot_teams",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("chatwoot_team_id", sa.Integer(), nullable=False), sa.Column("name", sa.String(160), nullable=False), sa.Column("last_synced_at", sa.String(40), nullable=False),
        sa.UniqueConstraint("tenant_id", "chatwoot_team_id"))
    op.create_table("chatwoot_labels",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("chatwoot_label_id", sa.Integer(), nullable=False), sa.Column("title", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False), sa.Column("color", sa.String(20), nullable=False),
        sa.Column("show_on_sidebar", sa.Boolean(), nullable=False), sa.Column("last_synced_at", sa.String(40), nullable=False),
        sa.UniqueConstraint("tenant_id", "chatwoot_label_id"), sa.UniqueConstraint("tenant_id", "title"))
    op.create_table("sync_jobs",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False), sa.Column("status", sa.String(30), nullable=False), sa.Column("phase", sa.String(40), nullable=False),
        sa.Column("current_page", sa.Integer(), nullable=False), sa.Column("total_items", sa.Integer(), nullable=False),
        sa.Column("completed_items", sa.Integer(), nullable=False), sa.Column("failed_items", sa.Integer(), nullable=False),
        sa.Column("stable_passes", sa.Integer(), nullable=False), sa.Column("error_code", sa.String(120)),
        sa.Column("created_at", sa.String(40), nullable=False), sa.Column("updated_at", sa.String(40), nullable=False), sa.Column("completed_at", sa.String(40)))
    op.create_index("ix_sync_jobs_status", "sync_jobs", ["status", "updated_at"])
    op.create_table("handoff_tasks",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("conversation_state_id", sa.Integer(), sa.ForeignKey("conversation_states.id"), nullable=False),
        sa.Column("reason_code", sa.String(80), nullable=False), sa.Column("reason_detail", sa.Text(), nullable=False), sa.Column("priority", sa.String(10), nullable=False),
        sa.Column("status", sa.String(30), nullable=False), sa.Column("assignee_user_id", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("chatwoot_assignee_id", sa.Integer()), sa.Column("sla_due_at", sa.String(40)), sa.Column("claimed_at", sa.String(40)),
        sa.Column("completed_at", sa.String(40)), sa.Column("completed_by", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("version", sa.Integer(), nullable=False), sa.Column("created_at", sa.String(40), nullable=False), sa.Column("updated_at", sa.String(40), nullable=False))
    op.create_index("ix_handoff_status_created", "handoff_tasks", ["status", "created_at"])
    op.create_table("notifications",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id")), sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("title", sa.String(200), nullable=False), sa.Column("body", sa.Text(), nullable=False), sa.Column("conversation_id", sa.Integer()),
        sa.Column("read_at", sa.String(40)), sa.Column("created_at", sa.String(40), nullable=False))
    op.create_index("ix_notifications_user_read", "notifications", ["user_id", "read_at"])
    op.create_table("notification_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("notification_id", sa.Integer(), sa.ForeignKey("notifications.id"), nullable=False),
        sa.Column("status", sa.String(30), nullable=False), sa.Column("attempts", sa.Integer(), nullable=False), sa.Column("available_at", sa.String(40), nullable=False),
        sa.Column("error_code", sa.String(120)), sa.Column("delivered_at", sa.String(40)))
    op.create_index("ix_notification_delivery_status", "notification_deliveries", ["status", "available_at"])
    op.create_table("sop_definitions",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False), sa.Column("description", sa.Text(), nullable=False), sa.Column("status", sa.String(30), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False), sa.Column("dry_run", sa.Boolean(), nullable=False), sa.Column("live_enabled", sa.Boolean(), nullable=False),
        sa.Column("trigger_type", sa.String(40), nullable=False), sa.Column("trigger_labels", sa.JSON(), nullable=False), sa.Column("inbox_ids", sa.JSON(), nullable=False),
        sa.Column("nodes", sa.JSON(), nullable=False), sa.Column("exit_labels", sa.JSON(), nullable=False), sa.Column("stop_on_incoming", sa.Boolean(), nullable=False),
        sa.Column("frequency_hours", sa.Integer(), nullable=False), sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False), sa.Column("updated_at", sa.String(40), nullable=False))
    op.create_table("sop_enrollments",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("sop_id", sa.Integer(), sa.ForeignKey("sop_definitions.id"), nullable=False),
        sa.Column("conversation_state_id", sa.Integer(), sa.ForeignKey("conversation_states.id"), nullable=False), sa.Column("status", sa.String(30), nullable=False),
        sa.Column("enrolled_at", sa.String(40), nullable=False), sa.Column("exited_at", sa.String(40)), sa.Column("exit_reason", sa.String(100)),
        sa.UniqueConstraint("sop_id", "conversation_state_id"))
    op.create_table("stored_media",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("original_name", sa.String(300), nullable=False), sa.Column("media_type", sa.String(30), nullable=False), sa.Column("mime_type", sa.String(120), nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=False), sa.Column("storage_path", sa.String(600), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False), sa.Column("created_at", sa.String(40), nullable=False))
    op.create_table("sop_jobs",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("enrollment_id", sa.Integer(), sa.ForeignKey("sop_enrollments.id"), nullable=False),
        sa.Column("node_key", sa.String(80), nullable=False), sa.Column("scheduled_at", sa.String(40), nullable=False), sa.Column("status", sa.String(30), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False), sa.Column("skip_reason", sa.String(120)), sa.Column("outbound_message_id", sa.Integer(), sa.ForeignKey("outbound_messages.id")),
        sa.Column("created_at", sa.String(40), nullable=False), sa.Column("completed_at", sa.String(40)), sa.UniqueConstraint("enrollment_id", "node_key"))
    op.create_index("ix_sop_jobs_due", "sop_jobs", ["status", "scheduled_at"])
    op.create_table("label_events",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("conversation_state_id", sa.Integer(), sa.ForeignKey("conversation_states.id"), nullable=False),
        sa.Column("label", sa.String(120), nullable=False), sa.Column("action", sa.String(20), nullable=False), sa.Column("created_at", sa.String(40), nullable=False))
    op.create_index("ix_label_events_created", "label_events", ["created_at"])
    op.create_table("audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id")), sa.Column("action", sa.String(100), nullable=False),
        sa.Column("resource_type", sa.String(80), nullable=False), sa.Column("resource_id", sa.String(120)), sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False))
    op.create_index("ix_audit_logs_created", "audit_logs", ["created_at"])


def downgrade() -> None:
    for table in ["audit_logs", "label_events", "sop_jobs", "stored_media", "sop_enrollments", "sop_definitions", "notification_deliveries", "notifications", "handoff_tasks", "sync_jobs", "chatwoot_labels", "chatwoot_teams", "chatwoot_agents"]:
        op.drop_table(table)
