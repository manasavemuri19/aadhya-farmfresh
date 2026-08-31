"""add delivery agent role support

Adds: agent location tracking on users (last_lat/last_lng/last_location_at),
and order->agent assignment (delivery_agent_id/delivery_assigned_at).
Assignment is deliberately independent of `status` — a delivery agent
accepting a request doesn't change what stage the order is in; staff still
drive packed/out_for_delivery/delivered on their own.

Revision ID: 0006_delivery_agent
Revises: 0005_support_tickets
Create Date: 2026-08-31 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0006_delivery_agent'
down_revision = '0005_support_tickets'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('last_lat', sa.Float(), nullable=True))
    op.add_column('users', sa.Column('last_lng', sa.Float(), nullable=True))
    op.add_column(
        'users', sa.Column('last_location_at', sa.DateTime(timezone=True), nullable=True)
    )

    op.add_column(
        'orders',
        sa.Column(
            'delivery_agent_id', sa.String(length=40),
            sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True,
        ),
    )
    op.add_column(
        'orders', sa.Column('delivery_assigned_at', sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index('ix_order_delivery_agent', 'orders', ['delivery_agent_id'])


def downgrade() -> None:
    op.drop_index('ix_order_delivery_agent', table_name='orders')
    op.drop_column('orders', 'delivery_assigned_at')
    op.drop_column('orders', 'delivery_agent_id')
    op.drop_column('users', 'last_location_at')
    op.drop_column('users', 'last_lng')
    op.drop_column('users', 'last_lat')
