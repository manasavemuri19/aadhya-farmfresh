"""add cash_settlements and cod_collections for AAD-BIZ-004's COD reconciliation

Two new tables, `cash_settlements` before `cod_collections` since the latter's
`settlement_id` is a foreign key into the former:

- cash_settlements: one row per settlement attempt for one delivery agent —
  expected (sum of that agent's unclaimed collections) vs. actual (what an
  admin recorded as physically received), a signed discrepancy, and a status
  that is `settled` only on an exact match, `discrepancy` otherwise (with a
  required reason — see CashService.settle_agent). Never edited after
  creation.
- cod_collections: one row per COD order, written only when
  OrderService.verify_delivery_code succeeds for a COD order.
  `order_id` is UNIQUE — a second, structural guard against double-recording
  one order's cash, on top of the delivery OTP already being single-use
  (AAD-SEC-027, migration 0022). `settlement_id` starts NULL ("pending") and
  is set exactly once, when an admin settles that agent's cash; a claimed
  collection is never reopened, which is also what makes a second settlement
  attempt against the same collections find nothing pending.

Revision ID: 0023_cod_reconciliation
Revises: 0022_order_delivery_otp
Create Date: 2026-09-25 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = '0023_cod_reconciliation'
down_revision = '0022_order_delivery_otp'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cash_settlements',
        sa.Column('id', sa.String(length=40), primary_key=True),
        sa.Column(
            'agent_id', sa.String(length=40),
            sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False,
        ),
        sa.Column('expected_amount_paise', sa.BigInteger(), nullable=False),
        sa.Column('actual_amount_paise', sa.BigInteger(), nullable=False),
        sa.Column('discrepancy_paise', sa.BigInteger(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('reason', sa.String(length=300), nullable=False, server_default=''),
        sa.Column(
            'recorded_by', sa.String(length=40),
            sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False,
        ),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.CheckConstraint(
            'expected_amount_paise >= 0', name='ck_settlement_expected_non_negative'
        ),
        sa.CheckConstraint('actual_amount_paise >= 0', name='ck_settlement_actual_non_negative'),
        sa.CheckConstraint(
            "status IN ('settled', 'discrepancy')", name='ck_settlement_status_valid'
        ),
    )
    op.create_index(
        'ix_cash_settlement_agent_created', 'cash_settlements', ['agent_id', 'created_at']
    )
    # AAD-DATA-013/0020: cash_settlements carries TimestampMixin, so it joins
    # the same DB-level updated_at trigger every other TimestampMixin table
    # has — set_updated_at() itself already exists from migration 0020.
    # cod_collections deliberately does not: it has no TimestampMixin (like
    # OrderLine/StockLedger, a child row scoped under its parent), and per
    # CashSettlement's own docstring a settlement row is never edited after
    # creation either — the trigger is added for the same "any write path"
    # completeness as every other TimestampMixin table, not because an
    # UPDATE is expected here.
    op.execute(
        """
        CREATE TRIGGER trg_cash_settlements_updated_at
        BEFORE UPDATE ON cash_settlements
        FOR EACH ROW
        EXECUTE FUNCTION set_updated_at();
        """
    )

    op.create_table(
        'cod_collections',
        sa.Column('id', sa.String(length=40), primary_key=True),
        sa.Column(
            'order_id', sa.String(length=40),
            sa.ForeignKey('orders.id', ondelete='RESTRICT'), nullable=False, unique=True,
        ),
        sa.Column(
            'agent_id', sa.String(length=40),
            sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False,
        ),
        sa.Column('amount_paise', sa.BigInteger(), nullable=False),
        sa.Column(
            'collected_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column(
            'settlement_id', sa.String(length=40),
            sa.ForeignKey('cash_settlements.id', ondelete='SET NULL'), nullable=True,
        ),
        sa.CheckConstraint('amount_paise > 0', name='ck_cod_collection_amount_positive'),
    )
    op.create_index(
        'ix_cod_collection_agent_pending', 'cod_collections', ['agent_id', 'settlement_id']
    )


def downgrade() -> None:
    op.drop_index('ix_cod_collection_agent_pending', table_name='cod_collections')
    op.drop_table('cod_collections')
    op.execute("DROP TRIGGER IF EXISTS trg_cash_settlements_updated_at ON cash_settlements;")
    op.drop_index('ix_cash_settlement_agent_created', table_name='cash_settlements')
    op.drop_table('cash_settlements')
