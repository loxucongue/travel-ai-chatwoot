"""add message content attributes

Revision ID: a91d4e7c2b65
Revises: fd31c8a6b204
Create Date: 2026-08-30 22:10:00
"""

from alembic import op
import sqlalchemy as sa


revision = "a91d4e7c2b65"
down_revision = "fd31c8a6b204"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "message_events",
        sa.Column("content_attributes", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.add_column(
        "outbound_messages",
        sa.Column("content_attributes", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )


def downgrade():
    op.drop_column("outbound_messages", "content_attributes")
    op.drop_column("message_events", "content_attributes")
