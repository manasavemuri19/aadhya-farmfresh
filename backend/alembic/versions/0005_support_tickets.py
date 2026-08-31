"""add support_tickets

The mailbox behind the Help & Support decision tree's "still stuck?"
fallback. Deliberately minimal — see the model's docstring.

Revision ID: 0005_support_tickets
Revises: 0004_checkout_payload
Create Date: 2026-08-31 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0005_support_tickets'
down_revision = '0004_checkout_payload'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'support_tickets',
        sa.Column('id', sa.String(length=40), primary_key=True),
        sa.Column('user_id', sa.String(length=40), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('context_node_id', sa.String(length=64), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
    )
    op.create_index('ix_support_ticket_user', 'support_tickets', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_support_ticket_user', table_name='support_tickets')
    op.drop_table('support_tickets')
