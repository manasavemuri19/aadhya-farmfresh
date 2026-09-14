"""token revocation: refresh_tokens table, users.status; drop dead otp_challenges

AAD-SEC-002: nothing issued could ever be invalidated — no logout, no way
to demote a compromised account, no way to force re-auth. This adds a
server-side record of every refresh token (`refresh_tokens`), so a single
token or an entire user's session family can actually be revoked, and a
`status` column on `users` so a suspended/deleted account is refused on
refresh and by the privileged-role dependencies, not just eventually once
someone deletes the row.

AAD-QUAL-001: phone+OTP login was retired in favour of Google sign-in; the
routes were removed but the repository, service methods, settings, id
helper, tests and this table were all left behind, still shipping in
production. Deleting it also resolves AAD-SEC-014 (the SMS-bombing and
timing-oracle holes that dead code carried, latent, as long as it existed).

Revision ID: 0010_refresh_tokens
Revises: 0009_payment_amount_mismatch
Create Date: 2026-09-14 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0010_refresh_tokens'
down_revision = '0009_payment_amount_mismatch'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('status', sa.String(length=16), nullable=False, server_default='active'),
    )

    op.create_table(
        'refresh_tokens',
        sa.Column('jti', sa.String(length=40), nullable=False),
        sa.Column('user_id', sa.String(length=40), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('issued_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('replaced_by', sa.String(length=40), nullable=True),
        sa.Column('device_label', sa.String(length=80), nullable=True),
        sa.Column('last_used_ip', sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('jti'),
    )
    op.create_index('ix_refresh_tokens_user', 'refresh_tokens', ['user_id'], unique=False)
    op.create_index('ix_refresh_tokens_expires', 'refresh_tokens', ['expires_at'], unique=False)

    op.drop_index('ix_otp_expires', table_name='otp_challenges')
    op.drop_index(op.f('ix_otp_challenges_phone'), table_name='otp_challenges')
    op.drop_table('otp_challenges')


def downgrade() -> None:
    op.create_table(
        'otp_challenges',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('phone', sa.String(length=16), nullable=False),
        sa.Column('code_hash', sa.Text(), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_otp_challenges_phone'), 'otp_challenges', ['phone'], unique=False)
    op.create_index('ix_otp_expires', 'otp_challenges', ['expires_at'], unique=False)

    op.drop_index('ix_refresh_tokens_expires', table_name='refresh_tokens')
    op.drop_index('ix_refresh_tokens_user', table_name='refresh_tokens')
    op.drop_table('refresh_tokens')

    op.drop_column('users', 'status')
