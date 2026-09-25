"""add orders.delivery_otp_* for AAD-SEC-027's in-app proof-of-delivery

In-app, phone-number-free delivery verification: when an order moves to
out_for_delivery, a 4-digit code is generated and shown to the customer in
their own (already-authenticated) app; the delivery agent enters it on
handoff and the backend checks it against `delivery_otp_hash` before letting
the order become delivered. Four columns:

- delivery_otp_hash: the Argon2 hash `verify_delivery_code` actually checks
  against — never `delivery_otp_plain`, so a DB-read compromise alone can't
  forge a delivery confirmation.
- delivery_otp_plain: the same code in the clear, short-lived and cleared the
  moment it's no longer needed (verified, expired, or the order left
  out_for_delivery some other way) — this is what the customer's own
  `GET /orders/{id}` re-displays on repeat visits during the delivery
  window. Storing it at all is a deliberate, disclosed trade-off (see
  OrderService's own comment at the call site) against never being able to
  show the code again after the screen that first generated it closes.
- delivery_otp_expires_at: bounds how long a code is usable — generated at
  dispatch, not order creation, so it never sits around for days.
- delivery_otp_attempts: a best-effort abuse counter (not CAS-protected —
  see record_delivery_otp_attempt's own comment), so a delivery agent's app
  can't just brute-force a 4-digit space.

Revision ID: 0022_order_delivery_otp
Revises: 0021_variant_low_stock_notified
Create Date: 2026-09-22 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0022_order_delivery_otp'
down_revision = '0021_variant_low_stock_notified'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('orders', sa.Column('delivery_otp_hash', sa.String(length=255), nullable=True))
    op.add_column('orders', sa.Column('delivery_otp_plain', sa.String(length=8), nullable=True))
    op.add_column(
        'orders', sa.Column('delivery_otp_expires_at', sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        'orders',
        sa.Column(
            'delivery_otp_attempts', sa.Integer(), nullable=False, server_default=sa.text('0')
        ),
    )


def downgrade() -> None:
    op.drop_column('orders', 'delivery_otp_attempts')
    op.drop_column('orders', 'delivery_otp_expires_at')
    op.drop_column('orders', 'delivery_otp_plain')
    op.drop_column('orders', 'delivery_otp_hash')
