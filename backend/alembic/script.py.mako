"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}

# AAD-DATA-012: adding an index with a plain op.create_index(...) takes a
# SHARE lock that blocks all writes to that table for as long as the build
# takes — fine on an empty or small table, a real outage on one with rows
# in it. If this migration adds an index to a table that already has
# production data by the time it runs, use postgresql_concurrently=True
# inside an autocommit block instead — see alembic/README.md for the
# pattern and why op.create_index alone isn't enough to opt into it.


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
