"""add payments.received_amount_paise for amount-mismatch handling

AAD-PAY-005: a capture webhook whose reported amount doesn't match the
order total used to be a log line and nothing else — the money stayed
captured at the gateway against a payment row that still said `created`,
and `_maybe_refund` (AAD-PAY-003) never fires for a status it doesn't
recognise. This column holds the amount the gateway actually reported
captured while `payments.status == 'amount_mismatch'`, so the periodic
sweep (`OrderService.process_amount_mismatches`) can refund exactly that
amount rather than the order's expected total.

Revision ID: 0009_payment_amount_mismatch
Revises: 0008_order_number_counters
Create Date: 2026-09-14 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0009_payment_amount_mismatch'
down_revision = '0008_order_number_counters'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'payments',
        sa.Column('received_amount_paise', sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('payments', 'received_amount_paise')
