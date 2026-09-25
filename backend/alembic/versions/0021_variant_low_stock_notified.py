"""add variants.low_stock_notified for AAD-BIZ-003's owner low-stock alert

The housekeeping sweep (app/main.py) runs every 2 minutes — without some
persisted "already told them" flag, a SKU sitting at 1 unit would generate a
fresh push to every staff/admin account roughly 30 times an hour until
someone restocked it. This column is that flag: set once a low-stock push
has gone out for a SKU, cleared once its stock next leaves the low-stock
band (restocked back above the threshold, or sold out to zero) so a later
dip notifies again instead of staying silently suppressed forever.

Revision ID: 0021_variant_low_stock_notified
Revises: 0020_updated_at_trigger
Create Date: 2026-09-22 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0021_variant_low_stock_notified'
down_revision = '0020_updated_at_trigger'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'variants',
        sa.Column(
            'low_stock_notified',
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column('variants', 'low_stock_notified')
