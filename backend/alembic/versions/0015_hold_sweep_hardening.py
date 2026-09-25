"""add a partial index for the hold sweeper's own query

AAD-REL-005: `find_expired_holds`'s predicate — status = 'pending_payment'
AND hold_expires_at < now() — had no supporting index. `ix_order_status_
created` is on `(status, created_at)`, which doesn't help a filter on
`hold_expires_at`, so this ran as a post-filter scan of every pending-
payment row. Partial rather than composite: `hold_expires_at` is only ever
meaningful on a `pending_payment` row (every other status has it cleared),
so indexing it for every other status's value would be dead weight.

The rest of that finding's fix (claiming rows one at a time with `FOR
UPDATE SKIP LOCKED` so replicas partition instead of colliding, and a
per-order SAVEPOINT so one order's failure doesn't roll back the whole
sweep) is a query/service-layer change with nothing for a migration to do —
see `OrderRepository.claim_expired_hold` and
`OrderService.release_expired_holds`.

Revision ID: 0015_hold_sweep_hardening
Revises: 0014_support_ticket_status
Create Date: 2026-09-16 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = '0015_hold_sweep_hardening'
down_revision = '0014_support_ticket_status'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        'ix_order_hold_expires_pending',
        'orders',
        ['hold_expires_at'],
        postgresql_where=sa.text("status = 'pending_payment'"),
    )


def downgrade() -> None:
    op.drop_index('ix_order_hold_expires_pending', table_name='orders')
