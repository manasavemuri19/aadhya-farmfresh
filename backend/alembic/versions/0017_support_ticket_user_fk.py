"""add support_tickets.user_id -> users.id foreign key

AAD-SEC-034: `support_tickets.user_id` was indexed but had no `ForeignKey`
to `users`. A ticket could reference a user id that never existed, or one
that has since been deleted, and nothing would notice — and the column
could not be reliably joined against `users` for an admin view. CASCADE
matches every other user-owned row in this schema (addresses, push_tokens):
if the account goes, its mailbox entries go with it. This app is pre-launch
with no production data, so no orphan cleanup pass is needed before adding
the constraint.

Revision ID: 0017_support_ticket_user_fk
Revises: 0016_payment_provider_order_unique
Create Date: 2026-09-18 00:00:00.000000
"""
from __future__ import annotations

from alembic import op

revision = '0017_support_ticket_user_fk'
down_revision = '0016_payment_provider_order_unique'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_foreign_key(
        'fk_support_ticket_user',
        'support_tickets',
        'users',
        ['user_id'],
        ['id'],
        ondelete='CASCADE',
    )


def downgrade() -> None:
    op.drop_constraint('fk_support_ticket_user', 'support_tickets', type_='foreignkey')
