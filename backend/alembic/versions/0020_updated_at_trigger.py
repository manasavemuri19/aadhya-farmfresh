"""add a BEFORE UPDATE trigger that maintains updated_at at the database

AAD-DATA-013: `TimestampMixin.updated_at` relies on SQLAlchemy's client-side
`onupdate=func.now()` — proven, by test, to actually fire for every
`update()` construct this codebase's repositories use (Core applies
`onupdate` to bulk `update()`, not just single-row ORM saves). The residual
gap is narrow but real: any write that reaches this table by a path other
than this application's own SQLAlchemy `update()` statements — a `psql` fix
during an incident, a one-off admin script, a future raw `text()` query —
silently leaves `updated_at` stale, because the guarantee lives in
application code that a different write path never runs. A `BEFORE UPDATE`
trigger moves the guarantee into the database itself: it fires on *any*
UPDATE to these tables, regardless of what wrote it, application code
included — the application's own `onupdate` is left in place rather than
removed (harmless redundancy: the trigger simply overwrites it with the
same `now()` the trigger itself computes, on every write).

Scoped to every table with `TimestampMixin` today: users, addresses,
categories, products, variants, orders, payments, push_tokens,
support_tickets.

Revision ID: 0020_updated_at_trigger
Revises: 0019_stock_ledger_order_fk
Create Date: 2026-09-21 00:00:00.000000
"""
from __future__ import annotations

from alembic import op

revision = '0020_updated_at_trigger'
down_revision = '0019_stock_ledger_order_fk'
branch_labels = None
depends_on = None

_TABLES = [
    'users', 'addresses', 'categories', 'products', 'variants',
    'orders', 'payments', 'push_tokens', 'support_tickets',
]


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    for table in _TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_updated_at
            BEFORE UPDATE ON {table}
            FOR EACH ROW
            EXECUTE FUNCTION set_updated_at();
            """
        )


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_updated_at ON {table};")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at();")
