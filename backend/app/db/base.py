"""Database engine and session lifecycle.

One engine per process, created on startup. Sessions are per-request and are
handed to repositories, so a single request's writes share one transaction and
commit or roll back together.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core import outbox
from app.core.config import settings

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Declarative base for every table."""


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def create_engine() -> AsyncEngine:
    return create_async_engine(
        settings.async_database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_s,
        pool_pre_ping=True,       # a connection killed by the DB is replaced, not raised
        pool_recycle=1800,        # managed Postgres drops idle connections; recycle first
        echo=settings.sql_echo,
        # AAD-PERF-002: previously unset, so one pathological query — a
        # missing index, a lock wait — held its connection indefinitely;
        # twenty of those and the pool is gone. asyncpg applies these as
        # session-level `SET`s on every new physical connection, so they
        # bound every statement/lock-wait this pool ever runs, not just the
        # request that happened to trigger the slow query.
        connect_args={
            "server_settings": {
                "statement_timeout": str(settings.db_statement_timeout_ms),
                "lock_timeout": str(settings.db_lock_timeout_ms),
                "idle_in_transaction_session_timeout": str(
                    settings.db_idle_in_transaction_timeout_ms
                ),
            }
        },
    )


async def connect() -> AsyncEngine:
    global _engine, _session_factory
    if _engine is None:
        _engine = create_engine()
        _session_factory = async_sessionmaker(
            _engine,
            class_=AsyncSession,
            expire_on_commit=False,   # objects stay usable after commit
            autoflush=False,          # flushes happen where we decide, not implicitly
        )
        log.info("database engine created")
    return _engine


async def disconnect() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        log.info("database engine disposed")


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("Database is not connected")
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope for background work and scripts.

    Commits on success, rolls back on any exception. Request handlers use the
    `db_session` dependency instead, which does the same thing per request.

    AAD-REL-004: the housekeeping sweeper's `_cancel` calls (main.py's
    `_run_sweep_once`, via `release_expired_holds`) go through this scope,
    not `TransactionalRoute` — so this is the other commit boundary that
    needs to open and drain an outbox batch, the same way, for the exact
    same reason: a push notification must never fire before the sweep's
    commit actually lands, and never at all if it doesn't.
    """
    session = get_session_factory()()
    outbox_token = outbox.start_batch()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    else:
        # Only on the success path — matches TransactionalRoute exactly:
        # nothing queued during this scope may fire on a rollback.
        await outbox.drain()
    finally:
        await session.close()
        outbox.end_batch(outbox_token)


async def ping() -> bool:
    from sqlalchemy import text

    async def _check() -> None:
        async with get_session_factory()() as session:
            await session.execute(text("SELECT 1"))

    try:
        # AAD-PERF-002: a *hung* database (not a down one — a connection
        # storm, a stuck lock) previously had no timeout here at all, so the
        # readiness probe itself would hang rather than fail. A hanging
        # probe is worse than a failing one: it can make the platform's own
        # health-check timeout the deciding factor instead of this code,
        # and it defeats the whole point of AAD-OPS-006's readiness/liveness
        # split, since a check that never returns never reports "degraded"
        # either.
        await asyncio.wait_for(_check(), timeout=2.0)
        return True
    except Exception:
        log.exception("database ping failed")
        return False
