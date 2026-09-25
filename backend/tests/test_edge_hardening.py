"""AAD-SEC-004 (rate limiting), AAD-SEC-005 (trusted client IP wiring),
AAD-SEC-006 (request-id validation) and AAD-SEC-013 (body size limit) —
real ASGI requests through the actual app, proving what a client on the
wire actually receives, following the pattern `test_idempotency_key_header.py`
established.

Each rate-limit test uses its own fake client IP (via X-Forwarded-For) so
the process-lifetime limiter singletons — the same design as the payment
provider's `@lru_cache` singleton `test_payment_endpoints.py` already works
around — don't carry state between tests. Each test patches the specific
limiter's `_limit` down to a small number rather than firing dozens of real
requests to exhaust a 60-or-120 window.
"""

from __future__ import annotations

import os
import pathlib

import httpx
import pytest

os.environ.setdefault(
    "DATABASE_URL",
    os.getenv(
        "TEST_DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/aadhya_test"
    ),
)
os.environ.setdefault("JWT_SECRET", "test-only-secret-at-least-32-characters-long")
os.environ.setdefault("PAYMENT_PROVIDER", "mock")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.api.deps import _global_authenticated_limiter  # noqa: E402
from app.api.v1.routes.auth import _google_per_minute, _refresh_per_minute  # noqa: E402
from app.api.v1.routes.catalog import _catalog_per_minute  # noqa: E402
from app.api.v1.routes.orders import _quote_per_minute  # noqa: E402
from app.core.ids import new_id  # noqa: E402
from app.core.security import issue_access_token  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.models import User as UserRow  # noqa: E402

TEST_DATABASE_URL = os.environ["DATABASE_URL"]

_ip_counter = iter(range(1, 255))


def _fresh_ip() -> str:
    return f"198.51.100.{next(_ip_counter)}"


@pytest.fixture
async def seeded_user():
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=None)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    user_id = new_id("usr", 12)
    async with factory() as session:
        session.add(
            UserRow(
                id=user_id, google_sub="edge_test_sub",
                email="edge@example.com", name="Edge Test",
            )
        )
        await session.commit()

    yield user_id
    await engine.dispose()


@pytest.fixture
async def asgi_client(seeded_user):
    from app.main import app as real_app

    async with real_app.router.lifespan_context(real_app):
        transport = httpx.ASGITransport(app=real_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


# ---------- AAD-SEC-004: rate limiting ----------


async def test_catalog_rate_limit_returns_429_with_retry_after(asgi_client, monkeypatch):
    monkeypatch.setattr(_catalog_per_minute, "_limit", 2)
    headers = {"x-forwarded-for": _fresh_ip()}

    r1 = await asgi_client.get("/v1/catalog", headers=headers)
    r2 = await asgi_client.get("/v1/catalog", headers=headers)
    r3 = await asgi_client.get("/v1/catalog", headers=headers)

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 429, r3.text
    assert r3.json()["error"]["code"] == "rate_limited"
    assert "retry-after" in {h.lower() for h in r3.headers}


async def test_catalog_rate_limit_is_independent_per_ip(asgi_client, monkeypatch):
    monkeypatch.setattr(_catalog_per_minute, "_limit", 1)
    headers_a = {"x-forwarded-for": _fresh_ip()}
    headers_b = {"x-forwarded-for": _fresh_ip()}

    r_a = await asgi_client.get("/v1/catalog", headers=headers_a)
    r_b = await asgi_client.get("/v1/catalog", headers=headers_b)

    assert r_a.status_code == 200
    assert r_b.status_code == 200, "a different client IP must not share the exhausted bucket"


async def test_cart_quote_rate_limit_returns_429(asgi_client, monkeypatch):
    """AAD-SEC-022: /cart/quote is deliberately open to signed-out callers,
    so it needs its own IP-keyed limiter rather than relying on
    _global_authenticated_limiter, which only ever sees a request that
    already carries a valid token."""
    monkeypatch.setattr(_quote_per_minute, "_limit", 2)
    headers = {"x-forwarded-for": _fresh_ip()}
    body = {"lines": [{"sku": "DOES-NOT-EXIST", "qty": 1}]}

    r1 = await asgi_client.post("/v1/cart/quote", json=body, headers=headers)
    r2 = await asgi_client.post("/v1/cart/quote", json=body, headers=headers)
    r3 = await asgi_client.post("/v1/cart/quote", json=body, headers=headers)

    assert r1.status_code == 200, r1.text  # unauthenticated — no token needed
    assert r2.status_code == 200
    assert r3.status_code == 429, r3.text
    assert r3.json()["error"]["code"] == "rate_limited"


async def test_refresh_rate_limit_returns_429(asgi_client, monkeypatch):
    monkeypatch.setattr(_refresh_per_minute, "_limit", 2)
    headers = {"x-forwarded-for": _fresh_ip()}
    body = {"refresh_token": "not-a-real-token"}

    r1 = await asgi_client.post("/v1/auth/refresh", json=body, headers=headers)
    r2 = await asgi_client.post("/v1/auth/refresh", json=body, headers=headers)
    r3 = await asgi_client.post("/v1/auth/refresh", json=body, headers=headers)

    assert r1.status_code == 401  # a garbage token, but it got past the limiter
    assert r2.status_code == 401
    assert r3.status_code == 429, r3.text


async def test_google_sign_in_rate_limit_returns_429(asgi_client, monkeypatch):
    monkeypatch.setattr(_google_per_minute, "_limit", 2)
    headers = {"x-forwarded-for": _fresh_ip()}
    body = {"id_token": "not-a-real-token"}

    r1 = await asgi_client.post("/v1/auth/google", json=body, headers=headers)
    r2 = await asgi_client.post("/v1/auth/google", json=body, headers=headers)
    r3 = await asgi_client.post("/v1/auth/google", json=body, headers=headers)

    assert r1.status_code == 401
    assert r2.status_code == 401
    assert r3.status_code == 429, r3.text


async def test_global_authenticated_limit_is_keyed_per_user(asgi_client, seeded_user, monkeypatch):
    monkeypatch.setattr(_global_authenticated_limiter, "_limit", 2)
    token = issue_access_token(seeded_user, role="customer")
    headers = {"Authorization": f"Bearer {token}"}

    r1 = await asgi_client.get("/v1/auth/me", headers=headers)
    r2 = await asgi_client.get("/v1/auth/me", headers=headers)
    r3 = await asgi_client.get("/v1/auth/me", headers=headers)

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 429, r3.text


# ---------- AAD-SEC-005: trusted client IP ----------


def test_dockerfile_no_longer_blanket_trusts_forwarded_headers():
    dockerfile = pathlib.Path(__file__).resolve().parents[1] / "Dockerfile"
    cmd_line = next(
        line for line in dockerfile.read_text().splitlines() if line.startswith("CMD ")
    )
    assert "--forwarded-allow-ips" not in cmd_line
    assert "--proxy-headers" not in cmd_line


# ---------- AAD-SEC-006: request-id validation ----------


async def test_a_well_formed_request_id_is_echoed_back(asgi_client):
    resp = await asgi_client.get(
        "/v1/catalog", headers={"X-Request-Id": "trace-abc-123456"}
    )
    assert resp.headers["X-Request-Id"] == "trace-abc-123456"


@pytest.mark.parametrize(
    "bad_id",
    ["short", "x" * 200, "has spaces here", "line\ninjection", "semi;colon"],
)
async def test_a_malformed_request_id_is_replaced_not_echoed(asgi_client, bad_id):
    resp = await asgi_client.get("/v1/catalog", headers={"X-Request-Id": bad_id})
    assert resp.headers["X-Request-Id"] != bad_id
    assert len(resp.headers["X-Request-Id"]) <= 64


# ---------- AAD-SEC-013: body size limit ----------


async def test_an_oversized_body_is_rejected_with_413(asgi_client):
    oversized = {"id_token": "x" * (2 * 1024 * 1024)}  # 2 MB, over the 1 MB cap
    resp = await asgi_client.post("/v1/auth/google", json=oversized)
    assert resp.status_code == 413, resp.text
    assert resp.json()["error"]["code"] == "payload_too_large"


async def test_a_normal_sized_body_is_unaffected(asgi_client):
    resp = await asgi_client.post("/v1/auth/google", json={"id_token": "a-normal-length-token"})
    assert resp.status_code != 413
