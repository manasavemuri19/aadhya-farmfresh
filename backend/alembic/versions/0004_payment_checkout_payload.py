"""add checkout_payload to payments

The gateway's hosted checkout URL (e.g. Razorpay's Payment Link short_url)
was previously only ever returned in the create-order response and never
saved — every later fetch of the order lost it, leaving the payment screen's
"Open payment page" permanently disabled with no way to complete a payment.

Revision ID: 0004_checkout_payload
Revises: 0003_google_auth
Create Date: 2026-08-30 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0004_checkout_payload'
down_revision = '0003_google_auth'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('payments', sa.Column('checkout_payload', postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column('payments', 'checkout_payload')
