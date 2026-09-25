"""make payments.provider_order_id unique

AAD-DATA-007: `ix_payment_provider_order` was a plain, non-unique index.
`OrderRepository.get_by_provider_order_id` resolves it with `.first()`, so
two rows that happened to share a provider order id would silently return
an arbitrary one instead of either being structurally impossible or
raising loudly. Postgres treats every NULL as distinct under a UNIQUE
index, so this does not constrain COD payments (which never get a
provider order id at all) — only the online payments that actually have
one, which is exactly the set this needs to guarantee is unique.

Revision ID: 0016_payment_provider_order_unique
Revises: 0015_hold_sweep_hardening
Create Date: 2026-09-18 00:00:00.000000
"""
from __future__ import annotations

from alembic import op

revision = '0016_payment_provider_order_unique'
down_revision = '0015_hold_sweep_hardening'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index('ix_payment_provider_order', table_name='payments')
    op.create_index(
        'ix_payment_provider_order',
        'payments',
        ['provider_order_id'],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index('ix_payment_provider_order', table_name='payments')
    op.create_index('ix_payment_provider_order', 'payments', ['provider_order_id'])
