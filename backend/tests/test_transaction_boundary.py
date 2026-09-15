"""Regression test for AAD-REL-001.

Every other test in this suite talks to services and repositories directly
(see conftest.py's own docstring: no test exercises an HTTP endpoint) — which
is exactly why this specific bug survived undetected. It lives in the gap
between "the service call succeeded" and "the HTTP response actually landed
on the client", and nothing below the HTTP layer can see that gap at all.

So this test is deliberately different from the rest of the suite: it boots
the real app on a real TCP socket and drives it with a real HTTP client. That
distinction is not cosmetic. httpx's in-process ASGI transport does not
reproduce this bug — a post-send exception there just propagates to the
calling test code as a raised exception, because there's no real, already-
flushed socket write for it to be "too late" for. Only a real socket models
what an actual customer sees: bytes the server already wrote are already
gone, whatever the server does next.

What's checked:
  * happy path — an order placed normally still returns 201 and is really
    there afterwards, read back through an independent session.
  * forced commit failure — the database write fails at the exact point
    AAD-REL-001 described (after the handler "succeeded"). Before the fix,
    the client received 200 with an order id for an order that didn't
    exist (see /home/claude/rel001/repro_mechanism.py for that reproduced
    against this exact FastAPI version, 0.141.1, isolated from app-specific
    plumbing). With the fix, the client must get a 5xx, and the order must
    genuinely not exist afterwards — no ghost order, no oversold stock.
"""
from __future__ import annotations

import asyncio
import os
import socket

import httpx
import pytest
import uvicorn
from fastapi import Request
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault(
    "DATABASE_URL",
    os.getenv(
        "TEST_DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/aadhya_test"
    ),
)
os.environ.setdefault("JWT_SECRET", "test-only-secret-at-least-32-characters-long")
os.environ.setdefault("PAYMENT_PROVIDER", "mock")

from app.api import deps  # noqa: E402
from app.core.ids import new_id  # noqa: E402
from app.core.security import issue_access_token  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.models import Category as CategoryRow  # noqa: E402
from app.db.models import User as UserRow  # noqa: E402
from app.repositories.products import ProductRepository  # noqa: E402
from app.schemas.catalog import Product, Variant  # noqa: E402

TEST_DATABASE_URL = os.environ["DATABASE_URL"]
SKU = "MILK-COW-1L-HTTP"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def seeded():
    """A clean schema plus exactly one buyable variant and one real user,
    committed for real — the live server started below opens its own
    sessions against this same database, so the data needs to actually be
    on disk, not just flushed inside a transaction this fixture is going to
    roll back."""
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=None)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    user_id = new_id("usr", 12)
    async with factory() as session:
        session.add(CategoryRow(slug="milk", name="Milk", sort_order=1, is_active=True))
        await session.flush()
        product = Product(
            id=new_id("prd", 12),
            slug="full-cream-cow-milk-http",
            name="Full Cream Cow Milk",
            description="Farm fresh",
            category="milk",
            prep_minutes=20,
            variants=[
                Variant(
                    sku=SKU, label="1 litre", pack_value=1, pack_unit="l",
                    price_paise=3500, mrp_paise=4000, stock_qty=5, max_per_order=10,
                ),
            ],
        )
        await ProductRepository(session).upsert_product(product)
        session.add(UserRow(id=user_id, google_sub="http_test_sub", email="http@example.com", name="HTTP Test"))
        await session.commit()

    yield user_id
    await engine.dispose()


@pytest.fixture
async def live_server(seeded):
    """The real app (app.main.app), served over a real loopback socket by
    uvicorn, running in this same process/event loop."""
    from app.main import app as real_app

    port = _free_port()
    config = uvicorn.Config(real_app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.02)
        else:
            raise RuntimeError("server did not start in time")
        yield f"http://127.0.0.1:{port}", seeded
    finally:
        server.should_exit = True
        await task
        real_app.dependency_overrides.pop(deps.db_session, None)


def _order_body() -> dict:
    return {
        "lines": [{"sku": SKU, "qty": 1}],
        "address": {
            "label": "Home", "line1": "12 Farm Road", "city": "Hyderabad", "pincode": "500001",
        },
        "payment_method": "cod",
    }


def _headers(token: str, *, idempotency_key: str) -> dict:
    # AAD-API-003: the header is required as of this fix — every real
    # request this file makes needs one, distinct per attempt except where
    # the test is deliberately reusing one (none here are).
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": idempotency_key}


async def test_happy_path_order_is_really_persisted(live_server):
    base_url, user_id = live_server
    token = issue_access_token(user_id, role="customer")

    async with httpx.AsyncClient(base_url=base_url, timeout=10) as client:
        resp = await client.post(
            "/v1/orders", json=_order_body(),
            headers=_headers(token, idempotency_key="happy-path-key-001"),
        )
        assert resp.status_code == 201, resp.text
        order_id = resp.json()["id"]

        # Read it back through a completely separate request/connection —
        # proves the write actually committed, not just that the first
        # response looked right.
        get_resp = await client.get(
            f"/v1/orders/{order_id}", headers={"Authorization": f"Bearer {token}"}
        )
        assert get_resp.status_code == 200
        assert get_resp.json()["id"] == order_id


async def test_forced_commit_failure_returns_5xx_not_200_and_writes_nothing(live_server):
    """The AAD-REL-001 regression test.

    Overrides db_session so this one server's session has its commit()
    replaced with something that always fails — simulating exactly the kind
    of failure the finding named (a deadlock, a serialization failure, a
    dropped connection at commit time), the same way the original
    reproduction did. Everything else about the request is completely
    normal: real routing, real dependency graph, real service and
    repository code, real stock-reservation SQL.
    """
    from app.main import app as real_app

    base_url, user_id = live_server
    token = issue_access_token(user_id, role="customer")

    class _CommitAlwaysFails(Exception):
        pass

    async def failing_db_session(request: Request):
        from app.db.base import get_session_factory

        session = get_session_factory()()
        request.state.db_session = session

        async def _commit():
            raise _CommitAlwaysFails("simulated: deadlock / serialization failure / dropped connection")

        session.commit = _commit  # instance-level monkeypatch, this session only
        yield session

    real_app.dependency_overrides[deps.db_session] = failing_db_session
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=10) as client:
            resp = await client.post(
                "/v1/orders", json=_order_body(),
                headers=_headers(token, idempotency_key="forced-failure-key-002"),
            )
            # The whole point of AAD-REL-001: this must NEVER be a 200/201
            # with a made-up order id for a write that didn't happen.
            assert resp.status_code >= 500, (
                f"expected a 5xx when the commit fails, got {resp.status_code}: {resp.text}"
            )
    finally:
        real_app.dependency_overrides.pop(deps.db_session, None)

    # And, independently of what the response said: prove nothing actually
    # persisted — no ghost order, and the stock reservation rolled back too.
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=None)
    async with engine.connect() as conn:
        from sqlalchemy import text

        order_count = (
            await conn.execute(text("SELECT count(*) FROM orders WHERE user_id = :u"), {"u": user_id})
        ).scalar_one()
        stock = (
            await conn.execute(text("SELECT stock_qty FROM variants WHERE sku = :s"), {"s": SKU})
        ).scalar_one()
    await engine.dispose()

    assert order_count == 0, "an order was persisted despite the commit failing"
    assert stock == 5, f"stock was left decremented ({stock}) despite the commit failing"

    # And the same live server, on the very next request (override removed,
    # exactly as a real deploy would be — no per-request state leaked, no
    # session left open blocking the pool), still places an order normally.
    async with httpx.AsyncClient(base_url=base_url, timeout=10) as client:
        resp = await client.post(
            "/v1/orders", json=_order_body(),
            headers=_headers(token, idempotency_key="post-failure-key-003"),
        )
        assert resp.status_code == 201, resp.text
