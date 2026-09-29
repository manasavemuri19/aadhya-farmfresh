"""add payments.refund_alerted_at for AAD-PAY-021's stuck-refund owner alert

The housekeeping sweep (app/main.py) retries a `refund_pending` payment's
gateway refund every 2 minutes, forever, and until now only logged each
failure — nothing anyone actually reads. AAD-PAY-021 pushes the owner once
a refund has been stuck for `STUCK_REFUND_ALERT_AFTER`. This column is the
"already told them" flag, same idea as variants.low_stock_notified (0021):
without it, a stuck refund would re-alert on every sweep pass. Set once, when
the alert goes out; left alone after — a refund that later succeeds simply
stops being `refund_pending` and drops out of the query on its own.

Revision ID: 0024_payment_refund_alerted
Revises: 0023_cod_reconciliation
Create Date: 2026-09-28 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0024_payment_refund_alerted'
down_revision = '0023_cod_reconciliation'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'payments',
        sa.Column('refund_alerted_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('payments', 'refund_alerted_at')
