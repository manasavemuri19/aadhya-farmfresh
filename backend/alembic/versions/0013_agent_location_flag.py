"""add users.last_location_flagged

AAD-SEC-028: a delivery agent's reported location is stored with no
plausibility check at all. This adds a column recording whether the most
recent update implied a physically impossible jump from the previous one
(see UserRepository.update_agent_location) -- flagged, not rejected, so a
suspicious report is still visible to review rather than silently accepted
with no trace.

Revision ID: 0013_agent_location_flag
Revises: 0012_catalog_audit
Create Date: 2026-09-15 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = '0013_agent_location_flag'
down_revision = '0012_catalog_audit'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column(
            'last_location_flagged', sa.Boolean(), nullable=False, server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column('users', 'last_location_flagged')
