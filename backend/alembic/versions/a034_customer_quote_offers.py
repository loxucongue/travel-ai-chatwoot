"""Persist a customer's first quotation deadline across conversations."""
from alembic import op
import sqlalchemy as sa

revision = 'a034_customer_quotes'
down_revision = 'a033_advisor_assignment'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('customer_quote_offers',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('subject_key', sa.String(160), nullable=False),
        sa.Column('route_variant', sa.String(100), nullable=False),
        sa.Column('offer', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.String(40), nullable=False),
        sa.UniqueConstraint('subject_key', 'route_variant'))


def downgrade():
    op.drop_table('customer_quote_offers')
