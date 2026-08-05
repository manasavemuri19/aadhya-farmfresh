"""Test fixtures.

These run against a **real PostgreSQL** database, not a stand-in. That matters
here more than usual: the guarantees this system depends on — the CHECK
constraint on stock, row-level locking during reservation, unique constraints
behind idempotency and webhook replay, savepoint behaviour on conflict — are
database behaviours. A mock would happily pass tests the real thing fails.

Each test gets a clean schema, created from the SQLAlchemy models.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.ids import new_id
from app.db.base import Base
from app.db.models import Category as CategoryRow
from app.domain.enums import StockPolicy
from app.payments.mock import MockPaymentProvider
from app.repositories.idempotency import IdempotencyRepository
from app.repositories.orders import OrderRepository
from app.repositories.products import ProductRepository
from app.schemas.catalog import Product, Variant
from app.services.order_service import OrderService

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/aadhya_test",
)


@pytest.fixture
async def engine():
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=None)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        yield session
        await session.rollback()


@pytest.fixture
def products(session) -> ProductRepository:
    return ProductRepository(session)


@pytest.fixture
def orders(session) -> OrderRepository:
    return OrderRepository(session)


@pytest.fixture
def order_service(session, products, orders) -> OrderService:
    return OrderService(
        products, orders, IdempotencyRepository(session), MockPaymentProvider()
    )


@pytest.fixture
async def user(session):
    """Orders reference users by foreign key, so tests need a real one."""
    from app.repositories.users import UserRepository

    record = await UserRepository(session).get_or_create_by_phone("+919876543210")
    await session.flush()
    return record


@pytest.fixture
async def categories(session):
    for slug, name in [("milk", "Milk"), ("paneer-khoya", "Paneer & Khoya")]:
        session.add(CategoryRow(slug=slug, name=name, sort_order=1, is_active=True))
    await session.flush()


@pytest.fixture
async def milk(session, products: ProductRepository, categories) -> Product:
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
            Variant(
                sku="MILK-COW-500ML", label="500 ml", pack_value=500, pack_unit="ml",
                price_paise=2000, stock_qty=0,
            ),
        ],
    )
    await products.upsert_product(product)
    await session.flush()
    return product


@pytest.fixture
async def khoya(session, products: ProductRepository, categories) -> Product:
    product = Product(
        id=new_id("prd", 12),
        slug="pure-khoya",
        name="Pure Khoya",
        description="Made to order",
        category="paneer-khoya",
        prep_minutes=35,
        variants=[
            Variant(
                sku="KHOYA-250G", label="250 g", pack_value=250, pack_unit="g",
                price_paise=14_000, stock_qty=0,
                stock_policy=StockPolicy.MADE_TO_ORDER, max_per_order=6,
            )
        ],
    )
    await products.upsert_product(product)
    await session.flush()
    return product
