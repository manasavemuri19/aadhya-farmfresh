"""drop orders.discount_paise

AAD-QUAL-015: `discount_paise` was hardcoded to `0` in `build_cart`
(`services/pricing.py`) and threaded through unchanged from there — the
schema, the API response, and this column. Nothing in this codebase has
ever computed a non-zero discount; there is no promotions system. Carrying
a column that can only ever hold its own default was pure noise on every
order read and write. If a promotion feature is ever built, a discount
column can come back alongside the logic that actually computes one.

Revision ID: 0018_drop_orders_discount_paise
Revises: 0017_support_ticket_user_fk
Create Date: 2026-09-21 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0018_drop_orders_discount_paise'
down_revision = '0017_support_ticket_user_fk'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column('orders', 'discount_paise')


def downgrade() -> None:
    op.add_column(
        'orders',
        sa.Column(
            'discount_paise', sa.BigInteger(), server_default='0', nullable=False
        ),
    )
