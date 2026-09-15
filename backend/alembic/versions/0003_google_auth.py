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
    # AAD-DATA-008: this used to re-apply `NOT NULL` and a UNIQUE index on
    # `users.phone`, both of which fail against any real database. Google
    # sign-in — the app's only login path — never supplies a phone number,
    # so every Google account has `phone IS NULL`; re-applying NOT NULL
    # raises NotNullViolation for every one of them. Phone was also
    # deliberately made non-unique in the upgrade (see the comment there) —
    # two unrelated households sharing a number, or one typo, would break
    # registration — so re-applying UNIQUE fails against any database that
    # has exercised that allowance.
    #
    # A downgrade that fails with a bare NotNullViolation at 2am tells you
    # nothing useful. This one tells you what actually happened and what to
    # do about it. Postgres runs DDL transactionally and Alembic uses that,
    # so raising here — same as the old failure — leaves the database
    # cleanly at head; nothing is left half-migrated.
    raise NotImplementedError(
        "0003_google_auth cannot be downgraded once Google accounts exist: "
        "Google sign-in never supplies a phone number, so re-applying "
        "NOT NULL on users.phone fails for every such account, and "
        "phone was deliberately made non-unique, so re-applying a unique "
        "index fails for any two accounts sharing a number. There is no "
        "code-level fix that doesn't lose data (backfilling a placeholder "
        "phone silently corrupts contact information). If you actually "
        "need to go back to the pre-Google-auth schema, restore from a "
        "pre-migration snapshot instead — see AAD-DATA-008 in "
        "PRR-AUDIT.md, and AAD-OPS-012 for why that restore path needs to "
        "be tested before you rely on it, not just documented."
    )
