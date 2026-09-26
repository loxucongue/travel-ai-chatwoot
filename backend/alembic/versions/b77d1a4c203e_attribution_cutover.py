"""initialize attribution and analytics cutover

Revision ID: b77d1a4c203e
Revises: a12f0c9d7e31
"""
from datetime import datetime, timezone

from alembic import op

revision = "b77d1a4c203e"
down_revision = "a12f0c9d7e31"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cutover = datetime.now(timezone.utc).isoformat()
    escaped = cutover.replace("'", "''")
    op.execute(f"UPDATE tenants SET analytics_cutover_at = '{escaped}' WHERE analytics_cutover_at IS NULL")
    op.execute("UPDATE message_events SET attribution = 'customer' WHERE direction = 'incoming' AND attribution = 'unknown'")
    op.execute("UPDATE message_events SET attribution = 'inferred_human' WHERE direction = 'outgoing' AND attribution = 'unknown'")
    op.execute("UPDATE message_events SET attribution = 'system' WHERE direction NOT IN ('incoming', 'outgoing') AND attribution = 'unknown'")
    op.execute("UPDATE message_events SET attribution = 'ai' WHERE chatwoot_message_id IN (SELECT chatwoot_message_id FROM outbound_messages WHERE source_type = 'ai' AND chatwoot_message_id IS NOT NULL)")


def downgrade() -> None:
    pass
