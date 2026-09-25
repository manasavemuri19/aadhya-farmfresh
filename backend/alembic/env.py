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
        # AAD-OPS-027: SQLAlchemy 2.0 "autobegins" a transaction on the very
        # first statement a Connection executes — including this advisory-
        # lock SELECT. If that autobegun transaction is still open when
        # alembic's own `context.configure()` runs, alembic sees the
        # connection already "in a transaction" and concludes the *caller*
        # must be managing it (`_in_external_transaction = True`), so its
        # `with context.begin_transaction():` recipe below silently becomes
        # a no-op — no BEGIN, no COMMIT, nothing. Every migration statement
        # still runs and still appears to succeed (no exception, full log
        # output), but nothing is ever committed: exiting `connectable.
        # connect()`'s `with` block issues an implicit ROLLBACK, and the
        # whole run vanishes as if it never happened. Confirmed by running
        # `upgrade head` against a throwaway empty database end-to-end and
        # then independently reconnecting: the log showed all 20 migrations
        # running clean, but a fresh connection found zero tables and no
        # `alembic_version` row at all. This predates AAD-OPS-026 entirely —
        # it's a property of the advisory-lock call added back in
        # AAD-OPS-002, so `alembic upgrade head` has likely never actually
        # persisted anything through this code path, in any environment,
        # for as long as that lock has existed; nothing before now checked
        # the post-migration database state independently of alembic's own
        # (misleadingly clean) log output. Committing here ends that
        # autobegun transaction — advisory locks are session-scoped, not
        # transactional, so they survive the commit — leaving the
        # connection transaction-free by the time alembic takes over, so
        # its own begin/commit around the real migration DDL works as
        # designed.
        connection.commit()
        try:
            # AAD-OPS-026: alembic's own bookkeeping table hard-codes
            # `version_num VARCHAR(32)` (alembic/ddl/impl.py) — this
            # project's revision ids are descriptive slugs, not alembic's
            # usual short hash, and `0016_payment_provider_order_unique` is
            # already 34 characters. Verified by running `upgrade head`
            # against a genuinely empty database: every migration's own DDL
            # is fine, right up until alembic tries to stamp that revision
            # id into a column too narrow to hold it, and the whole chain
            # dies with StringDataRightTruncation — a fresh deploy's very
            # first migration run would never reach head. `CREATE TABLE ...
            # IF NOT EXISTS` here, before alembic gets a chance to create
            # its own narrower one, means alembic finds a table already
            # present and never touches its column type; the `ALTER ...
            # TYPE` covers an environment where a narrower table already
            # exists from before this fix (a no-op if it's already wide
            # enough — VARCHAR widening is instant, no table rewrite).
            connection.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS alembic_version ("
                    "version_num VARCHAR(255) NOT NULL, "
                    "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
                )
            )
            connection.execute(
                text("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(255)")
            )
            # AAD-OPS-027, continued: same autobegin problem as above — the
            # two DDL statements just above reopen a transaction the moment
            # they run, so it has to be closed again before alembic gets
            # the connection, or we're right back to `_in_external_transaction`.
            connection.commit()
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
