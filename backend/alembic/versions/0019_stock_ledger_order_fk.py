"""add stock_ledger.order_id -> orders.id foreign key

AAD-DATA-014: `stock_ledger.order_id` was indexed but had no `ForeignKey` to
`orders`, despite this table's whole purpose being auditability. A typo'd
or stale order_id would sit there forever with nothing to notice. SET NULL
rather than CASCADE: nothing in this codebase ever hard-deletes an `orders`
row today, but the ledger's audit value should outlive the order it
references if that ever changes, rather than disappearing with it. This
app is pre-launch with no production data, so no orphan cleanup pass is
needed before adding the constraint.

Revision ID: 0019_stock_ledger_order_fk
Revises: 0018_drop_orders_discount_paise
Create Date: 2026-09-21 00:00:00.000000
"""
from __future__ import annotations

from alembic import op

revision = '0019_stock_ledger_order_fk'
down_revision = '0018_drop_orders_discount_paise'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_foreign_key(
        'fk_stock_ledger_order',
        'stock_ledger',
        'orders',
        ['order_id'],
        ['id'],
        ondelete='SET NULL',
    )


def downgrade() -> None:
    op.drop_constraint('fk_stock_ledger_order', 'stock_ledger', type_='foreignkey')
