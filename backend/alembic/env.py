"""Alembic environment.

The database URL comes from application settings rather than alembic.ini, so
there is exactly one place a connection string is configured and no chance of
migrating a different database than the app talks to.
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool, text

from alembic import context
from app.core.config import settings
from app.db import models  # noqa: F401  — imported so Base.metadata is populated
from app.db.base import Base

# AAD-OPS-002: `alembic upgrade head` is now run as a single gated pre-deploy
# step (railway.json's `preDeployCommand`), which closes the multi-replica
# race this key issue was about. This lock is a second, independent layer:
# it makes the command itself safe to run concurrently, in case it's ever
# invoked outside that gated path — a manual run during an incident, a
# different deploy target that doesn't support a pre-deploy step. Session-
# scoped (`pg_advisory_lock`, not the transaction-scoped variant) and held
# for the whole connection, released explicitly in `finally` rather than
# relying on the connection closing, so a failed migration still frees it
# for the next attempt. The key is an arbitrary constant unique to this job.
_MIGRATION_ADVISORY_LOCK_KEY = 88230002

config = context.config
config.set_main_option("sqlalchemy.url", settings.sync_database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.sync_database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        connection.execute(
            text("SELECT pg_advisory_lock(:key)"), {"key": _MIGRATION_ADVISORY_LOCK_KEY}
        )
        try:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
            )
            with context.begin_transaction():
                context.run_migrations()
        finally:
            connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": _MIGRATION_ADVISORY_LOCK_KEY}
            )


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
