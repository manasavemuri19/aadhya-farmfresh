"""Database engine and session lifecycle.

One engine per process, created on startup. Sessions are per-request and are
handed to repositories, so a single request's writes share one transaction and
commit or roll back together.
"""

from __future__ import annotations

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

from app.core.config import settings

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Declarative base for every table."""


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def create_engine() -> AsyncEngine:
    return create_async_engine(
        settings.async_database_url,
        pool_size=10,
        max_overflow=10,
        pool_pre_ping=True,       # a connection killed by the DB is replaced, not raised
        pool_recycle=1800,        # managed Postgres drops idle connections; recycle first
        echo=settings.sql_echo,
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
    """
    session = get_session_factory()()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def ping() -> bool:
    from sqlalchemy import text

    try:
        async with get_session_factory()() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        log.exception("database ping failed")
        return False
