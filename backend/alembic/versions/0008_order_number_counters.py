"""add order_number_counters table

AAD-DATA-001: the human-readable order number was six random digits with no
collision check — a 50% chance of a collision after roughly 1,180 orders,
which at 100 orders/day is twelve days. This table backs a daily-scoped
sequence instead: one row per calendar date, incremented atomically with
`INSERT ... ON CONFLICT DO UPDATE ... RETURNING` (see
`OrderRepository.next_order_number`), producing `AD-YYMMDD-NNNN` — collision-
free by construction, and more useful operationally, since the rider and
support can read the date straight off the number.

Revision ID: 0008_order_number_counters
Revises: 0007_push_tokens
Create Date: 2026-09-14 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0008_order_number_counters'
down_revision = '0007_push_tokens'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'order_number_counters',
        sa.Column('order_date', sa.Date(), primary_key=True),
        sa.Column('last_value', sa.Integer(), nullable=False, server_default='0'),
    )


def downgrade() -> None:
    op.drop_table('order_number_counters')
