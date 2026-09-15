"""add catalog_audit table

AAD-DATA-017: price changes, availability toggles and the bulk seed/sync
path all mutated commercial fields (price, MRP, stock policy, per-order cap,
availability) with no record of what changed, who changed it, or when.
`stock_ledger` already does this for quantity; this is the same idea for
everything else.

Revision ID: 0012_catalog_audit
Revises: 0011_relax_mrp_and_enum_checks
Create Date: 2026-09-14 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = '0012_catalog_audit'
down_revision = '0011_relax_mrp_and_enum_checks'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'catalog_audit',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('sku', sa.String(length=48), nullable=False),
        sa.Column('field', sa.String(length=32), nullable=False),
        sa.Column('old_value', sa.Text(), nullable=True),
        sa.Column('new_value', sa.Text(), nullable=True),
        sa.Column('actor', sa.String(length=40), nullable=False, server_default='system'),
        sa.Column('source', sa.String(length=16), nullable=False, server_default='admin_api'),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        'ix_catalog_audit_sku_created', 'catalog_audit', ['sku', 'created_at'],
    )


def downgrade() -> None:
    op.drop_index('ix_catalog_audit_sku_created', table_name='catalog_audit')
    op.drop_table('catalog_audit')
