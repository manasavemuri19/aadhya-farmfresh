"""AAD-PERF-001 — product detail and search now opt out of the
`Cache-Control: no-store` default the same way `/catalog` already does, and
carry a strong ETag so a repeat fetch with a matching `If-None-Match` gets
an empty 304 instead of the full body again.

Real ASGI requests through the actual app, following the pattern
`test_edge_hardening.py` established (a real Postgres schema, a real
`httpx.ASGITransport` client) rather than calling the service layer
directly — the thing actually being verified here is what a client on the
wire receives, which only a real HTTP round trip can prove.
"""

from __future__ import annotations

import os

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

from app.core.ids import new_id  # noqa: E402
from app.db.models import Category as CategoryRow  # noqa: E402
from app.repositories.products import ProductRepository  # noqa: E402
from app.schemas.catalog import Product, Variant  # noqa: E402

TEST_DATABASE_URL = os.environ["DATABASE_URL"]


@pytest.fixture
async def seeded_product(_clean_schema):
    # Depends on conftest.py's autouse `_clean_schema` explicitly so pytest
    # resolves that TRUNCATE *before* this fixture's own insert, not after
    # — the two are otherwise both function-scoped with no dependency
    # link, and nothing else guarantees which runs first.
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=None)

    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    product = Product(
        id=new_id("prd", 12),
        slug="full-cream-cow-milk",
        name="Full Cream Cow Milk",
        description="Farm fresh",
        category="milk",
        prep_minutes=20,
        variants=[
            Variant(
                sku="MILK-COW-1L", label="1 litre", pack_value=1, pack_unit="l",
                price_paise=3500, mrp_paise=4000, stock_qty=5, max_per_order=10,
            ),
        ],
    )
    async with factory() as session:
        session.add(CategoryRow(slug="milk", name="Milk", sort_order=1, is_active=True))
        await session.flush()
        await ProductRepository(session).upsert_product(product)
        await session.commit()

    yield product
    await engine.dispose()


@pytest.fixture
async def asgi_client(seeded_product):
    from app.main import app as real_app

    async with real_app.router.lifespan_context(real_app):
        transport = httpx.ASGITransport(app=real_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


# ---------- product detail ----------


async def test_product_detail_opts_out_of_no_store(asgi_client, seeded_product):
    r = await asgi_client.get(f"/v1/catalog/products/{seeded_product.slug}")

    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=60, stale-while-revalidate=300"
    assert "etag" in {h.lower() for h in r.headers}


async def test_product_detail_etag_is_stable_across_calls(asgi_client, seeded_product):
    r1 = await asgi_client.get(f"/v1/catalog/products/{seeded_product.slug}")
    r2 = await asgi_client.get(f"/v1/catalog/products/{seeded_product.slug}")

    assert r1.headers["etag"] == r2.headers["etag"]


async def test_product_detail_matching_if_none_match_gets_empty_304(asgi_client, seeded_product):
    first = await asgi_client.get(f"/v1/catalog/products/{seeded_product.slug}")
    etag = first.headers["etag"]

    second = await asgi_client.get(
        f"/v1/catalog/products/{seeded_product.slug}", headers={"if-none-match": etag}
    )

    assert second.status_code == 304
    assert second.content == b""
    # 304 must still carry the same validators, so a client's cache stays fresh.
    assert second.headers["etag"] == etag
    assert second.headers["cache-control"] == "public, max-age=60, stale-while-revalidate=300"


async def test_product_detail_stale_if_none_match_still_returns_full_body(asgi_client, seeded_product):
    r = await asgi_client.get(
        f"/v1/catalog/products/{seeded_product.slug}",
        headers={"if-none-match": '"not-the-real-one"'},
    )

    assert r.status_code == 200
    assert r.json()["slug"] == seeded_product.slug


# ---------- search ----------


async def test_search_opts_out_of_no_store(asgi_client, seeded_product):
    r = await asgi_client.get("/v1/catalog/search", params={"q": "milk"})

    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=60, stale-while-revalidate=300"
    assert "etag" in {h.lower() for h in r.headers}


async def test_search_matching_if_none_match_gets_empty_304(asgi_client, seeded_product):
    first = await asgi_client.get("/v1/catalog/search", params={"q": "milk"})
    etag = first.headers["etag"]

    second = await asgi_client.get(
        "/v1/catalog/search", params={"q": "milk"}, headers={"if-none-match": etag}
    )

    assert second.status_code == 304
    assert second.content == b""


async def test_search_etag_changes_with_the_result_set(asgi_client, seeded_product):
    milk = await asgi_client.get("/v1/catalog/search", params={"q": "milk"})
    nothing = await asgi_client.get("/v1/catalog/search", params={"q": "zzz-no-match"})

    assert milk.headers["etag"] != nothing.headers["etag"]


# ---------- /catalog itself is unchanged ----------


async def test_catalog_list_still_uses_its_own_short_window_and_no_etag(asgi_client, seeded_product):
    r = await asgi_client.get("/v1/catalog")

    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=30"
    # /catalog stamps generated_at on every call, so it deliberately has no
    # ETag — see AAD-PERF-001's own write-up on why that stays as is.
    assert "etag" not in {h.lower() for h in r.headers}
