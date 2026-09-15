"""Regression test for AAD-REL-002.

A malformed `Idempotency-Key` header is a client formatting bug, not a
failed authentication. Before this fix, `app.api.deps.idempotency_key`
raised `Unauthorized` (401) for a key outside 8-128 characters. The mobile
client (`client.ts`) treats *any* 401 on an authenticated call as an expired
access token: it refreshes, and per AAD-MOB-001 can clear the keychain — so
a malformed key turned into the customer being signed out mid-checkout, with
the inevitable retry sending the same bad key and failing the same way
again. It must come back as a 422 instead, so the client fixes the header
rather than the session.

Runs over a real ASGI request through the actual app (FastAPI dependency
resolution, the real `Unauthorized`/`ValidationError` classes, the real
registered exception handler) rather than calling `idempotency_key()`
directly, so it proves what the client actually receives on the wire. This
is a pre-response header check with no post-send timing involved, so
(unlike AAD-REL-001's regression test) httpx's in-process ASGI transport
reproduces it fully — no real socket is needed here.
"""
from __future__ import annotations

import os

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault(
    "DATABASE_URL",
    os.getenv(
        "TEST_DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/aadhya_test"
    ),
)
os.environ.setdefault("JWT_SECRET", "test-only-secret-at-least-32-characters-long")
os.environ.setdefault("PAYMENT_PROVIDER", "mock")

from app.core.ids import new_id  # noqa: E402
from app.core.security import issue_access_token  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.models import Category as CategoryRow  # noqa: E402
from app.db.models import User as UserRow  # noqa: E402
from app.repositories.products import ProductRepository  # noqa: E402
from app.schemas.catalog import Product, Variant  # noqa: E402

TEST_DATABASE_URL = os.environ["DATABASE_URL"]
SKU = "MILK-COW-1L-IDEMP"


@pytest.fixture
async def seeded():
    """A real, committed user and a buyable variant — the ASGI-transport
    client below drives the real app, which opens its own session against
    this same database."""
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
            slug="full-cream-cow-milk-idemp",
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
        session.add(
            UserRow(
                id=user_id, google_sub="idemp_test_sub",
                email="idemp@example.com", name="Idemp Test",
            )
        )
        await session.commit()

    yield user_id
    await engine.dispose()


@pytest.fixture
async def asgi_client(seeded):
    from app.main import app as real_app

    token = issue_access_token(seeded, role="customer")
    # db_session depends on a pool the app's lifespan connects on startup —
    # ASGITransport alone never runs it, so drive it explicitly.
    async with real_app.router.lifespan_context(real_app):
        transport = httpx.ASGITransport(app=real_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            client.headers["Authorization"] = f"Bearer {token}"
            yield client


def _order_body() -> dict:
    return {
        "lines": [{"sku": SKU, "qty": 1}],
        "address": {
            "label": "Home", "line1": "12 Farm Road", "city": "Hyderabad", "pincode": "500001",
        },
        "payment_method": "cod",
    }


@pytest.mark.parametrize("bad_key", ["short", "x" * 129, "  "])
async def test_malformed_idempotency_key_is_422_not_401(asgi_client, bad_key):
    resp = await asgi_client.post(
        "/v1/orders", json=_order_body(), headers={"Idempotency-Key": bad_key}
    )
    assert resp.status_code == 422, resp.text
    assert resp.status_code != 401
    assert resp.json()["error"]["code"] == "validation_error"


async def test_well_formed_idempotency_key_is_unaffected(asgi_client):
    resp = await asgi_client.post(
        "/v1/orders", json=_order_body(), headers={"Idempotency-Key": "a" * 36}
    )
    assert resp.status_code == 201, resp.text


async def test_missing_idempotency_key_is_rejected_as_422_not_401(asgi_client):
    """AAD-API-003: the header is required, not optional — a missing key is
    the same failure mode as a malformed one (a client bug that would
    otherwise create a duplicate order on retry), so it gets the same 422
    validation_error rather than a distinct error shape, and — just as with
    a malformed key — must never come back as a 401. See this file's module
    docstring for why a 401 specifically is the wrong failure here."""
    resp = await asgi_client.post("/v1/orders", json=_order_body())
    assert resp.status_code == 422, resp.text
    assert resp.status_code != 401
    assert resp.json()["error"]["code"] == "validation_error"
