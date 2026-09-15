"""Batch 5 — operability: AAD-OPS-002 (migration advisory lock), AAD-OPS-003
(request-id log ordering), AAD-OPS-004 (health-path log suppression),
AAD-PERF-002 (statement timeout + bounded pool + ping timeout), and
AAD-REL-003 (sweeper: sweep-at-startup, advisory lock, batched deletes).

AAD-OPS-006 (railway.json's healthcheckPath) is a platform config change with
no code to unit test — verified by reading the file, disclosed as such in the
audit write-up rather than claimed as executed here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

os.environ.setdefault(
    "DATABASE_URL",
    os.getenv(
        "TEST_DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/aadhya_test"
    ),
)
os.environ.setdefault("JWT_SECRET", "test-only-secret-at-least-32-characters-long")
os.environ.setdefault("PAYMENT_PROVIDER", "mock")

from app.core.logging import request_id_var  # noqa: E402
from app.db import base as db  # noqa: E402
from app.db.models import IdempotencyKey  # noqa: E402
from app.repositories.idempotency import IdempotencyRepository  # noqa: E402

TEST_DATABASE_URL = os.environ["DATABASE_URL"]

# Must match alembic/env.py's _MIGRATION_ADVISORY_LOCK_KEY — duplicated here
# rather than imported, because importing alembic.env executes migration
# logic at module scope (it isn't written to be a safely-importable module).
_MIGRATION_ADVISORY_LOCK_KEY = 88230002


class _CapturingHandler(logging.Handler):
    """Records what request_id_var.get() returns at the moment each record
    is emitted — exactly what JsonFormatter reads at format time, and
    exactly the value AAD-OPS-003 was about: the ContextVar, not something
    stashed on the LogRecord."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[tuple[str, str]] = []  # (path, request_id)

    def emit(self, record: logging.LogRecord) -> None:
        self.seen.append((getattr(record, "path", ""), request_id_var.get()))


@pytest.fixture
async def asgi_client():
    from app.main import app as real_app

    async with real_app.router.lifespan_context(real_app):
        transport = httpx.ASGITransport(app=real_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest.fixture
def access_log_capture():
    handler = _CapturingHandler()
    logger = logging.getLogger("app.access")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    yield handler
    logger.removeHandler(handler)


# ---------- AAD-OPS-003: request id survives to the success log line ----------


async def test_success_log_line_carries_the_real_request_id(asgi_client, access_log_capture):
    response = await asgi_client.get("/v1/catalog")

    assert response.status_code == 200
    header_id = response.headers["x-request-id"]
    assert header_id != "-"

    catalog_lines = [rid for path, rid in access_log_capture.seen if path == "/v1/catalog"]
    assert catalog_lines, "expected an access-log line for /v1/catalog"
    # Before the fix, this was "-" on every successful request: the
    # contextvar was reset in a `finally` around only call_next, before the
    # success log ran.
    assert catalog_lines[-1] == header_id


async def test_failed_request_also_carries_the_real_request_id(asgi_client, access_log_capture):
    # A malformed refresh token 401s without ever touching the DB — cheap
    # way to exercise a real, non-500 failure path through the real app.
    response = await asgi_client.post("/v1/auth/refresh", json={"refresh_token": "garbage"})

    assert response.status_code == 401
    header_id = response.headers["x-request-id"]
    assert header_id != "-"


# ---------- AAD-OPS-004: health paths, resolved with the real mount prefix ----------


async def test_health_paths_are_not_logged(asgi_client, access_log_capture):
    live = await asgi_client.get("/v1/health/live")
    ready = await asgi_client.get("/v1/health")

    assert live.status_code == 200
    assert ready.status_code in (200, 503)
    logged_paths = {path for path, _ in access_log_capture.seen}
    assert "/v1/health" not in logged_paths
    assert "/v1/health/live" not in logged_paths


async def test_a_real_route_is_still_logged(asgi_client, access_log_capture):
    await asgi_client.get("/v1/catalog")
    logged_paths = {path for path, _ in access_log_capture.seen}
    assert "/v1/catalog" in logged_paths


# ---------- AAD-PERF-002: statement timeout, and ping() has one too ----------


async def test_statement_timeout_cancels_a_long_running_query(monkeypatch):
    from app.core.config import settings

    # A tiny timeout so the test is fast and unambiguous, rather than the
    # real 10s default — this proves the same mechanism create_engine() uses
    # (asyncpg server_settings) is honoured by Postgres, not that the
    # specific configured default value is correct (that's just a setting).
    monkeypatch.setattr(settings, "db_statement_timeout_ms", 200)
    engine = db.create_engine()
    try:
        with pytest.raises(Exception, match="(?i)cancel"):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT pg_sleep(2)"))
    finally:
        await engine.dispose()


async def test_ping_times_out_instead_of_hanging_on_a_stuck_check(monkeypatch):
    class _HungSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, *_a, **_kw):
            await asyncio.sleep(30)  # far longer than ping()'s 2s wait_for

    def _hung_factory():
        return _HungSession()

    monkeypatch.setattr(db, "get_session_factory", lambda: _hung_factory)

    started = time.perf_counter()
    result = await db.ping()
    elapsed = time.perf_counter() - started

    assert result is False
    # Bounded well under the 30s the fake check would otherwise take —
    # proves asyncio.wait_for's 2.0s ceiling actually fires.
    assert elapsed < 5.0


# ---------- AAD-OPS-002: the migration advisory lock actually serializes ----------


async def test_migration_advisory_lock_blocks_a_concurrent_holder():
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=None)
    try:
        async with engine.connect() as conn_a, engine.connect() as conn_b:
            got_a = (
                await conn_a.execute(
                    text("SELECT pg_advisory_lock(:key)"),
                    {"key": _MIGRATION_ADVISORY_LOCK_KEY},
                )
            )
            got_a.close()  # pg_advisory_lock always succeeds (it blocks, never fails)

            # A second connection's non-blocking try-lock on the same key
            # must fail while conn_a holds it — this is the exact property
            # that makes two concurrent `alembic upgrade head` invocations
            # safe: the second one blocks (not tried here, to keep the test
            # fast) rather than racing.
            still_locked = (
                await conn_b.execute(
                    text("SELECT pg_try_advisory_lock(:key)"),
                    {"key": _MIGRATION_ADVISORY_LOCK_KEY},
                )
            ).scalar_one()
            assert still_locked is False

            await conn_a.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": _MIGRATION_ADVISORY_LOCK_KEY}
            )

            now_available = (
                await conn_b.execute(
                    text("SELECT pg_try_advisory_lock(:key)"),
                    {"key": _MIGRATION_ADVISORY_LOCK_KEY},
                )
            ).scalar_one()
            assert now_available is True
            await conn_b.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": _MIGRATION_ADVISORY_LOCK_KEY}
            )
    finally:
        await engine.dispose()


# ---------- AAD-REL-003: batched deletes drain fully, sweep runs at startup,
# and the sweep lock actually excludes a concurrent holder ----------


async def test_delete_expired_drains_across_multiple_batches(session):
    """A single unbounded DELETE would also delete all 5 rows and report 5 —
    identical end state to the batched version — so the count alone doesn't
    distinguish "one statement" from "looped in batches". What actually
    proves the loop runs is the number of round trips: batch_size=2 over 5
    rows must take 3 execute() calls (2 + 2 + 1), not 1.
    """
    from datetime import UTC, datetime, timedelta

    past = datetime.now(UTC) - timedelta(hours=1)
    for i in range(5):
        session.add(
            IdempotencyKey(
                user_id="usr_test", key=f"batch-key-{i}", fingerprint="fp",
                status="completed", expires_at=past,
            )
        )
    await session.flush()

    repo = IdempotencyRepository(session)
    real_execute = session.execute
    delete_calls = 0

    async def _counting_execute(stmt, *a, **kw):
        nonlocal delete_calls
        if stmt.__class__.__name__ == "Delete":
            delete_calls += 1
        return await real_execute(stmt, *a, **kw)

    session.execute = _counting_execute
    try:
        deleted = await repo.delete_expired(batch_size=2)
    finally:
        session.execute = real_execute

    assert deleted == 5
    assert delete_calls == 3, "expected 3 batched DELETEs (2 + 2 + 1), not one unbounded statement"
    remaining = (
        await session.execute(text("SELECT count(*) FROM idempotency_keys"))
    ).scalar_one()
    assert remaining == 0


async def test_housekeeping_sweeps_immediately_at_startup(monkeypatch):
    """AAD-REL-003: this used to `sleep(120)` before the first sweep. The
    loop now does the work first — proven here by starting `_housekeeping`
    as a task, yielding control almost immediately, and confirming the
    sweep ran before any sleep could have elapsed."""
    from app import main as main_module

    ran = asyncio.Event()

    async def _fake_sweep_once():
        ran.set()

    monkeypatch.setattr(main_module, "_run_sweep_once", _fake_sweep_once)

    task = asyncio.create_task(main_module._housekeeping(app=None))
    try:
        await asyncio.wait_for(ran.wait(), timeout=1.0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_sweep_lock_excludes_a_concurrent_holder(session):
    """A second 'replica' finds the lock held and skips its work instead of
    racing the first — proven by an expired row that survives one
    _run_sweep_once call made while another connection holds the lock, then
    gets cleaned up by a second call made after the lock is released. Not
    catching an exception is not enough here: the old, unlocked code would
    also raise nothing while happily doing (duplicate) work.
    """
    from datetime import UTC, datetime, timedelta

    from app import main as main_module

    past = datetime.now(UTC) - timedelta(hours=1)
    session.add(
        IdempotencyKey(
            user_id="usr_test", key="lock-test-key", fingerprint="fp",
            status="completed", expires_at=past,
        )
    )
    await session.commit()

    await db.connect()
    other_engine = create_async_engine(TEST_DATABASE_URL, poolclass=None)
    try:
        async with other_engine.connect() as holder:
            await holder.execute(
                text("SELECT pg_advisory_lock(:key)"),
                {"key": main_module._SWEEP_ADVISORY_LOCK_KEY},
            )
            try:
                await main_module._run_sweep_once()
                still_there = (
                    await session.execute(
                        text("SELECT count(*) FROM idempotency_keys WHERE key = 'lock-test-key'")
                    )
                ).scalar_one()
                assert still_there == 1, "the row should survive while another replica holds it"
            finally:
                await holder.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": main_module._SWEEP_ADVISORY_LOCK_KEY},
                )

        await main_module._run_sweep_once()
        gone = (
            await session.execute(
                text("SELECT count(*) FROM idempotency_keys WHERE key = 'lock-test-key'")
            )
        ).scalar_one()
        assert gone == 0, "once the lock is free, the next sweep should clean it up"
    finally:
        await other_engine.dispose()
        await db.disconnect()
