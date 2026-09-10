"""add push_tokens table

Stores one row per registered device push token (Expo push token), used to
send order-status and new-delivery-request notifications. Keyed by the
token itself, not an autoincrement id: re-registering the same token (a
reinstall, or the same device signing in as a different account) just
repoints it at the new user rather than accumulating stale duplicate rows —
see PushTokenRepository.register.

Revision ID: 0007_push_tokens
Revises: 0006_delivery_agent
Create Date: 2026-09-10 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0007_push_tokens'
down_revision = '0006_delivery_agent'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'push_tokens',
        sa.Column('token', sa.String(length=200), primary_key=True),
        sa.Column(
            'user_id', sa.String(length=40),
            sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False,
        ),
        sa.Column('platform', sa.String(length=16), nullable=False, server_default='android'),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index('ix_push_token_user', 'push_tokens', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_push_token_user', table_name='push_tokens')
    op.drop_table('push_tokens')
