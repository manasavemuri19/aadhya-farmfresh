"""add google sign-in fields, phone optional

Revision ID: 8a795156aaee
Revises: 0002_otp_grace
Create Date: 2026-08-28 12:16:15.378536
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0003_google_auth'
down_revision = '0002_otp_grace'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('google_sub', sa.String(length=64), nullable=True))
    op.add_column('users', sa.Column('email', sa.String(length=120), nullable=True))
    op.alter_column('users', 'phone',
               existing_type=sa.VARCHAR(length=16),
               nullable=True)
    op.create_index(op.f('ix_users_google_sub'), 'users', ['google_sub'], unique=True)
    # phone stops being a login identity here and becomes plain delivery
    # contact info — it must not stay UNIQUE, or two unrelated households
    # sharing a number (or one typo) would break registration outright.
    op.drop_index(op.f('ix_users_phone'), table_name='users')
    op.create_index(op.f('ix_users_phone'), 'users', ['phone'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_users_phone'), table_name='users')
    op.create_index(op.f('ix_users_phone'), 'users', ['phone'], unique=True)
    op.drop_index(op.f('ix_users_google_sub'), table_name='users')
    op.alter_column('users', 'phone',
               existing_type=sa.VARCHAR(length=16),
               nullable=False)
    op.drop_column('users', 'email')
    op.drop_column('users', 'google_sub')
