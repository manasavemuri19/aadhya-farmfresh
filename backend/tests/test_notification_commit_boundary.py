"""AAD-REL-004 — proves the wiring at the commit-boundary level itself
(`TransactionalRoute`), not just the outbox mechanism or OrderService's use
of it: a push notification queued during a real HTTP request must not have
gone out while the transaction is still open, and must never go out at all
if the commit then fails — the exact scenario the finding named ("a push
for an order that turned out not to exist").
"""

from __future__ import annotations

from fastapi import Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api import deps
from app.core.ids import new_id
from app.core.security import issue_access_token
from app.db.models import Category as CategoryRow
from app.db.models import User as UserRow
from app.domain.enums import Role
from app.repositories.products import ProductRepository
from app.schemas.catalog import Product, Variant

SKU = "MILK-COW-1L-OUTBOX"


async def _seed(engine) -> tuple[str, str]:
    """A buyable variant, one customer, and one delivery agent, committed
    for real — same shape as test_http_layer.py's `seed`, plus an agent so
    the COD path's `_notify_agents_new_order` actually has someone to
    notify."""
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    user_id = new_id("usr", 12)
    agent_id = new_id("usr", 12)
    async with factory() as session:
        session.add(CategoryRow(slug="milk", name="Milk", sort_order=1, is_active=True))
        await session.flush()
        product = Product(
            id=new_id("prd", 12),
            slug="full-cream-cow-milk-outbox",
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
                id=user_id, google_sub="outbox_cust_sub",
                email="outboxc@example.com", name="Cust",
            )
        )
        session.add(
            UserRow(
                id=agent_id, google_sub="outbox_agent_sub", email="outboxa@example.com",
                name="Agent", role=Role.DELIVERY_AGENT.value,
            )
        )
        await session.commit()
    return user_id, agent_id


def _order_body() -> dict:
    return {
        "lines": [{"sku": SKU, "qty": 1}],
        "address": {
            "label": "Home", "line1": "1 Farm Road", "city": "Hyderabad", "pincode": "500001",
        },
        "payment_method": "cod",
    }


async def test_a_real_http_request_drains_push_only_after_commit_succeeds(engine, monkeypatch):
    from app.main import app as real_app
    from app.services.push_service import PushService

    calls = []

    async def fake_notify_users(self, user_ids, *, title, body, data=None):
        calls.append((list(user_ids), title))

    monkeypatch.setattr(PushService, "notify_users", fake_notify_users)

    user_id, _agent_id = await _seed(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async def override_db_session(request: Request):
        session = factory()
        request.state.db_session = session
        yield session

    real_app.dependency_overrides[deps.db_session] = override_db_session
    transport = ASGITransport(app=real_app, raise_app_exceptions=False)
    token = issue_access_token(user_id, role="customer")

    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/v1/orders", json=_order_body(),
                headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "outbox-http-001"},
            )
            assert resp.status_code == 201, resp.text
    finally:
        real_app.dependency_overrides.pop(deps.db_session, None)

    # By the time the HTTP response has come back, the batch has already
    # been drained inside TransactionalRoute — the push already happened.
    assert calls, "expected the agent notification to have been sent after a successful commit"


async def test_a_forced_commit_failure_means_the_push_never_goes_out(engine, monkeypatch):
    """The direct AAD-REL-004 regression: same request as above, but the
    commit is forced to fail (the exact AAD-REL-001 scenario) — the push
    must never fire for an order that, from the database's point of view,
    never happened."""
    from app.main import app as real_app
    from app.services.push_service import PushService

    calls = []

    async def fake_notify_users(self, user_ids, *, title, body, data=None):
        calls.append((list(user_ids), title))

    monkeypatch.setattr(PushService, "notify_users", fake_notify_users)

    user_id, _agent_id = await _seed(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    class _CommitAlwaysFailsError(Exception):
        pass

    async def failing_db_session(request: Request):
        session = factory()
        request.state.db_session = session

        async def _commit():
            raise _CommitAlwaysFailsError("simulated: deadlock / serialization failure")

        session.commit = _commit
        yield session

    real_app.dependency_overrides[deps.db_session] = failing_db_session
    transport = ASGITransport(app=real_app, raise_app_exceptions=False)
    token = issue_access_token(user_id, role="customer")

    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/v1/orders", json=_order_body(),
                headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "outbox-http-002"},
            )
            assert resp.status_code >= 500, resp.text
    finally:
        real_app.dependency_overrides.pop(deps.db_session, None)

    assert calls == [], "a push must never fire for a transaction whose commit failed"


async def test_draining_a_push_after_commit_does_not_leak_the_connection(engine, monkeypatch):
    """Pins the exact mechanism behind the drain-ordering bug that was found
    via tests/test_payment_endpoints.py: `outbox.drain()` used to run AFTER
    `session.close()`, so `PushTokenRepository.list_for_users()` (issued
    from inside the drained push effect) silently reopened a transaction on
    the already-closed-but-reusable session — never cleaned up, leaking
    until Postgres's `idle_in_transaction_session_timeout` killed it 15s
    later. That only showed up as a stall between sequential requests under
    a real app lifespan, which is slow and awkward to assert on directly.
    This test instead checks the mechanism deterministically: once the
    response for an order that triggers a real (non-monkeypatched) push
    lookup has come back, the connection must already be back in the pool —
    not leaked into a next, unclosed transaction."""
    user_id, _agent_id = await _seed(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    from app.main import app as real_app

    async def override_db_session(request: Request):
        session = factory()
        request.state.db_session = session
        yield session

    real_app.dependency_overrides[deps.db_session] = override_db_session
    transport = ASGITransport(app=real_app, raise_app_exceptions=False)
    token = issue_access_token(user_id, role="customer")

    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/v1/orders", json=_order_body(),
                headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "outbox-http-003"},
            )
            assert resp.status_code == 201, resp.text
    finally:
        real_app.dependency_overrides.pop(deps.db_session, None)

    assert engine.pool.checkedout() == 0, (
        "a connection was left checked out after the response was returned — "
        "outbox.drain() is reusing a session after it was closed"
    )
