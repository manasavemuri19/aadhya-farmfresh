"""add support_tickets.status

AAD-BIZ-005: `SupportRepository` had exactly one method, `create` — no
`list`, no `get`, no admin endpoint, no status field, so a ticket that
arrived was indistinguishable, from the system's point of view, from one
that was never sent. This adds the status field the admin read path
(`GET /admin/support/tickets`, `routes/admin.py`) filters and paginates on:
`open` by default, `closed` once a staff account has dealt with it. A CHECK
constraint follows the same closed-set convention `AAD-DATA-010` established
for every other status-like column in this schema. The new index supports
the admin queue's oldest-open-first query the same way `ix_order_status_
created` supports the order queue.

Revision ID: 0014_support_ticket_status
Revises: 0013_agent_location_flag
Create Date: 2026-09-16 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = '0014_support_ticket_status'
down_revision = '0013_agent_location_flag'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'support_tickets',
        sa.Column('status', sa.String(length=16), nullable=False, server_default='open'),
    )
    op.create_check_constraint(
        'ck_support_ticket_status_valid', 'support_tickets', "status IN ('open', 'closed')",
    )
    op.create_index(
        'ix_support_ticket_status_created', 'support_tickets', ['status', 'created_at'],
    )


def downgrade() -> None:
    op.drop_index('ix_support_ticket_status_created', table_name='support_tickets')
    op.drop_constraint('ck_support_ticket_status_valid', 'support_tickets', type_='check')
    op.drop_column('support_tickets', 'status')
