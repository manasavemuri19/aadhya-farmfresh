"""AAD-REL-007 — discovered while writing AAD-OPS-020's first real
HTTP-layer route tests, not while looking for it: a route test on
`GET /orders/{id}` for a missing order (an ordinary 404) left the test
engine's connection pool one connection short, confirmed directly against
`engine.pool.checkedout()`.

`TransactionalRoute.transactional_handler` (api/route.py) had exactly one
`try/finally` that owned the request's session — rollback/close — and it
sat entirely *below* `response = await original_route_handler(request)`.
Any exception raised while building that response (a business `AppError`
like a 404, a 422 validation error, an unhandled 500) skipped the block
completely: the session was never rolled back, never closed, just
permanently checked out of the pool. `db_session` (api/deps.py)
deliberately does nothing after its own `yield` — this class is where that
lifecycle is supposed to live — so nothing else in FastAPI's own
dependency-teardown chain was ever going to catch it either.

This is worse than `AAD-REL-001`, not milder: 401s, 404s and 422s are the
*common* shape of API traffic, not a rare race, so this leaked a connection
on a large fraction of all requests and would exhaust a production pool far
sooner than AAD-REL-001's exact commit-timing race ever would.
"""

from __future__ import annotations

from fastapi import Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api import deps
from app.core.security import issue_access_token


async def test_an_error_response_returns_its_connection_to_the_pool(engine):
    """The direct regression test: several requests that each end in a
    business AppError (a 404 — the order genuinely doesn't exist) must not
    leave the pool's checked-out count climbing. Before the fix this failed
    outright: checked-out count grew by one on every iteration and the pool
    (size 5 by default) was exhausted well before 8 requests."""
    from app.main import app as real_app

    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async def override_db_session(request: Request):
        session = factory()
        request.state.db_session = session
        yield session

    real_app.dependency_overrides[deps.db_session] = override_db_session
    transport = ASGITransport(app=real_app, raise_app_exceptions=False)
    token = issue_access_token("nonexistent_user_for_this_test", role="customer")

    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            for i in range(8):
                resp = await client.get(
                    f"/v1/orders/ord_does_not_exist_{i}",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert resp.status_code == 404
                assert engine.pool.checkedout() == 0, (
                    f"connection leaked on request {i}: "
                    f"{engine.pool.checkedout()} still checked out"
                )
    finally:
        real_app.dependency_overrides.pop(deps.db_session, None)


async def test_a_validation_error_response_also_returns_its_connection(engine):
    """A 422 (missing Idempotency-Key) goes through a different dependency
    — idempotency_key — before the route body ever runs, but the same
    TransactionalRoute wraps it. Confirms the fix isn't accidentally scoped
    to only the AppError case."""
    from app.main import app as real_app

    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async def override_db_session(request: Request):
        session = factory()
        request.state.db_session = session
        yield session

    real_app.dependency_overrides[deps.db_session] = override_db_session
    transport = ASGITransport(app=real_app, raise_app_exceptions=False)
    token = issue_access_token("nonexistent_user_for_this_test_2", role="customer")

    order_body = {
        "lines": [{"sku": "DOES-NOT-MATTER", "qty": 1}],
        "address": {
            "label": "Home", "line1": "1 Farm Road", "city": "Hyderabad", "pincode": "500001",
        },
        "payment_method": "cod",
    }

    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            for _ in range(6):
                resp = await client.post(
                    "/v1/orders", json=order_body, headers={"Authorization": f"Bearer {token}"}
                )
                assert resp.status_code == 422
                assert engine.pool.checkedout() == 0
    finally:
        real_app.dependency_overrides.pop(deps.db_session, None)
