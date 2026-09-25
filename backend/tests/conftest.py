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
from sqlalchemy import create_engine, event, text
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


@pytest.fixture(scope="session", autouse=True)
def _build_schema_once():
    """AAD-OPS-023: this schema rebuild (`drop_all` + `create_all`) used to
    run once per test — real DDL, ~590 times a run. Now it runs exactly
    once per test session; `_clean_schema` below gives each test its
    isolation instead, without redoing DDL for it.

    Deliberately a **plain sync fixture using a throwaway sync engine**,
    not an async, session-scoped version of the `engine` fixture below.
    Tried that first and hit pytest-asyncio's known pitfall head-on: a
    session-scoped async fixture and function-scoped test coroutines don't
    share an event loop by default, so an asyncpg connection opened while
    building the schema gets used from a *different* loop by the first
    test and asyncpg raises `RuntimeError: Future attached to a different
    loop`. Fixable with more pytest-asyncio configuration (`loop_scope`
    plumbing throughout), but a plain sync engine sidesteps event loops
    entirely for a one-time, one-shot piece of DDL that doesn't need to be
    async in the first place — simpler and has nothing to get wrong.
    """
    sync_url = TEST_DATABASE_URL.replace("postgresql+asyncpg", "postgresql+psycopg")
    sync_engine = create_engine(sync_url)
    Base.metadata.drop_all(sync_engine)
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()


@pytest.fixture
async def engine():
    """Stays function-scoped deliberately — see `_build_schema_once` above
    for why a session-scoped *async* engine doesn't work safely here. This
    fixture no longer does any DDL (the schema already exists by the time
    any test runs); it just opens a connection to it, which is cheap.
    """
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=None)
    yield engine
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _clean_schema(engine):
    """AAD-OPS-023: gives every test a clean slate now that the schema is
    built once per run rather than once per test (`_build_schema_once`).

    Deliberately `TRUNCATE ... RESTART IDENTITY CASCADE` between tests
    rather than the more common savepoint-per-test pattern (begin a
    transaction, hand tests a session bound to it, roll back at teardown
    instead of committing). That pattern would break a large and important
    part of this suite: the real-concurrency tests (`test_concurrency_real.py`,
    Batch 20's race test in `test_batch20_hygiene.py`) and every HTTP-layer
    test (`test_http_layer.py`'s `client` fixture gives each simulated
    request its own real session) all depend on a genuinely *separate*
    connection seeing genuinely *committed* data — that's the whole point
    of what they're testing. A savepoint rolled back at teardown is
    invisible to any other connection, which would make all of that
    machinery silently test nothing. TRUNCATE keeps the same "writes really
    commit, a second connection really sees them" architecture those tests
    need, while still resetting every table (and every SERIAL sequence, via
    `RESTART IDENTITY`) before each test runs — the same effective
    per-test starting state `drop_all`/`create_all` always gave, just far
    cheaper to produce.
    """
    async with engine.begin() as conn:
        table_names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
        await conn.execute(text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE"))
    yield


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
def idempotency(session) -> IdempotencyRepository:
    return IdempotencyRepository(session)


class QueryCounter:
    """AAD-OPS-025: counts real round trips to the database, not Python-
    level repository calls — `session.add()` alone never shows up here,
    only an actual statement sent over the connection does. `.count` is
    mutable so a test can reset it between two calls it wants to compare
    (e.g. "does a bigger cart issue more queries than a smaller one")."""

    def __init__(self) -> None:
        self.count = 0


@pytest.fixture
def query_counter(engine) -> QueryCounter:
    counter = QueryCounter()

    def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        counter.count += 1

    event.listen(engine.sync_engine, "before_cursor_execute", _before_cursor_execute)
    yield counter
    event.remove(engine.sync_engine, "before_cursor_execute", _before_cursor_execute)


@pytest.fixture
def order_service(session, products, orders) -> OrderService:
    return OrderService(
        products, orders, IdempotencyRepository(session), MockPaymentProvider()
    )


@pytest.fixture
async def user(session):
    """Orders reference users by foreign key, so tests need a real one.

    Uses the Google sign-in path, matching how real accounts are created
    now — order-flow tests don't care which auth mechanism made the user,
    they just need a real row to reference.
    """
    from app.repositories.users import UserRepository

    record = await UserRepository(session).get_or_create_by_google(
        google_sub="test_google_sub_0001", email="test@example.com", name="Test User",
    )
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
