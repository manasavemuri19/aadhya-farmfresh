"""AAD-OPS-020 — until this file, no test in the suite ever called an HTTP
endpoint (`tests/conftest.py`'s own docstring flags it) or exercised the app
through its real ASGI/ routing/middleware/exception-handler stack — every
other test calls services and repositories directly. `test_transaction_boundary.py`
is the one existing exception, and only for the one bug (AAD-REL-001) that
specifically needs a real socket rather than an in-process ASGI transport;
its own docstring explains why.

This file is the general-purpose version: a `client` fixture (`httpx.AsyncClient`
over `ASGITransport`, real routing/middleware/exception handlers, a `db_session`
override that commits for real against the same test-schema engine the rest
of the suite uses) plus a `seed` fixture that commits fixture data through a
*separate* session on that same engine — because each HTTP request opens its
own fresh session via the override, seeing only what has already actually
committed, exactly like two different requests in production would.

Covers the concrete gaps the finding names: a request with no
`Idempotency-Key` header is rejected at the real validation layer
(AAD-API-003), a malformed one is a 422 not the old 401 (AAD-REL-002), an
unauthenticated request is a clean 401, a successful request's access-log
line carries a real request id rather than the AAD-OPS-003 regression's
`"-"`, and the generic exception handler never leaks a traceback. Plus the
core structural gap itself: an order placed over real HTTP is genuinely
there afterwards, read back through a second, independent request.
"""

from __future__ import annotations

import io
import json
import logging

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api import deps
from app.core.ids import new_id
from app.core.logging import JsonFormatter
from app.core.security import issue_access_token
from app.db.models import Category as CategoryRow
from app.db.models import User as UserRow
from app.repositories.products import ProductRepository
from app.schemas.catalog import Product, Variant

SKU = "MILK-COW-1L-HTTP2"


@pytest.fixture
async def client(engine):
    """A real ASGI client against the real app — real routing, both real
    middlewares, the real exception handlers, and a `db_session` override
    that commits for real (through `TransactionalRoute`, unchanged) against
    the same per-test engine the rest of the suite already uses. Not a
    substitute for `test_transaction_boundary.py`'s real-socket fixture:
    that file's own docstring explains why only a real socket reproduces
    AAD-REL-001's specific bytes-already-sent timing. This fixture is for
    everything else an HTTP request touches that a direct service call
    doesn't: status codes, response shapes, header behaviour, and the
    exception-handling middleware.
    """
    from app.main import app as real_app

    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async def override_db_session(request: Request):
        session = factory()
        request.state.db_session = session
        yield session

    real_app.dependency_overrides[deps.db_session] = override_db_session
    # raise_app_exceptions=False: httpx's default re-raises an unhandled
    # exception into the test instead of letting FastAPI's own registered
    # exception handler convert it to a response — the opposite of what a
    # real deployed server does. Without this, test_unhandled_exception_...
    # below would see the raw RuntimeError instead of handle_unexpected's
    # 500 JSON body, which isn't what AAD-OPS-020 is testing.
    transport = ASGITransport(app=real_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
    real_app.dependency_overrides.pop(deps.db_session, None)


@pytest.fixture
async def seed(engine):
    """One buyable variant and one real user, committed for real on the same
    engine `client` talks to — each HTTP request opens its own fresh
    session and only ever sees data that has already actually committed,
    same as two separate requests would in production."""
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    user_id = new_id("usr", 12)
    async with factory() as session:
        session.add(CategoryRow(slug="milk", name="Milk", sort_order=1, is_active=True))
        await session.flush()
        product = Product(
            id=new_id("prd", 12),
            slug="full-cream-cow-milk-http2",
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
                id=user_id, google_sub="http2_test_sub",
                email="http2@example.com", name="HTTP Test",
            )
        )
        await session.commit()
    return user_id


def _order_body() -> dict:
    return {
        "lines": [{"sku": SKU, "qty": 1}],
        "address": {
            "label": "Home", "line1": "12 Farm Road", "city": "Hyderabad", "pincode": "500001",
        },
        "payment_method": "cod",
    }


async def test_health_endpoints_are_reachable_over_real_http(client):
    live = await client.get("/v1/health/live")
    assert live.status_code == 200
    assert live.json()["status"] == "ok"

    # Readiness pings the *global* db module (app/db/base.py), not the
    # per-test engine this fixture's own override uses — it is deliberately
    # not wired up here, so this only asserts the endpoint responds with the
    # documented shape (AAD-OPS-006's ok/degraded contract), not a specific
    # status code, which depends on state this test doesn't control.
    ready = await client.get("/v1/health")
    assert ready.status_code in (200, 503)
    body = ready.json()
    assert body["status"] in ("ok", "degraded")
    assert "database" in body["checks"]


async def test_unauthenticated_request_is_a_clean_401_not_a_500(client):
    resp = await client.get("/v1/orders")
    assert resp.status_code == 401
    body = resp.json()
    assert "error" in body


async def test_order_creation_without_idempotency_key_is_422_over_http(client, seed):
    """AAD-API-003 at the HTTP boundary: a real request, real header
    validation, real exception-handling middleware — not just the direct
    dependency-function call the domain-level tests already cover."""
    token = issue_access_token(seed, role="customer")
    resp = await client.post(
        "/v1/orders", json=_order_body(), headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


async def test_a_malformed_idempotency_key_is_422_not_401_over_http(client, seed):
    """AAD-REL-002 at the HTTP boundary: a malformed header used to produce
    a 401, which client.ts's refresh-on-401 logic treats as an expired
    session — turning a client bug into a spurious sign-out. Confirms the
    real route now returns 422 like any other bad input."""
    token = issue_access_token(seed, role="customer")
    resp = await client.post(
        "/v1/orders", json=_order_body(),
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "short"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


async def test_placing_an_order_over_http_is_really_there_on_a_second_request(client, seed):
    """The core structural gap AAD-OPS-020 names: a write made through one
    HTTP request must be visible to a completely separate one — proving the
    commit genuinely happened, not just that the first response looked
    right."""
    token = issue_access_token(seed, role="customer")
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "http-layer-key-001"}

    create_resp = await client.post("/v1/orders", json=_order_body(), headers=headers)
    assert create_resp.status_code == 201, create_resp.text
    order_id = create_resp.json()["id"]
    assert create_resp.json()["status"] == "confirmed"  # COD confirms instantly

    get_resp = await client.get(
        f"/v1/orders/{order_id}", headers={"Authorization": f"Bearer {token}"}
    )
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == order_id


async def test_getting_a_nonexistent_order_is_a_well_shaped_404(client, seed):
    token = issue_access_token(seed, role="customer")
    resp = await client.get(
        "/v1/orders/ord_does_not_exist_at_all", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"]
    assert "message" in body["error"]


async def test_a_404_carries_the_same_request_id_as_its_own_response_header(client, seed):
    """AAD-QUAL-010: previously only the catch-all 500 handler attached a
    request_id to the error body — every ordinary AppError (this 404
    included) returned an envelope without one, so a customer could quote a
    request id from a crash but not from the far more common "that thing
    doesn't exist" response."""
    token = issue_access_token(seed, role="customer")
    resp = await client.get(
        "/v1/orders/ord_does_not_exist_at_all", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["request_id"]
    assert body["error"]["request_id"] == resp.headers["X-Request-Id"]


async def test_an_unauthenticated_401_also_carries_a_request_id(client):
    resp = await client.get("/v1/orders")
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["request_id"]
    assert body["error"]["request_id"] == resp.headers["X-Request-Id"]


async def test_a_request_validation_422_also_carries_a_request_id(client, seed):
    """The one error path that isn't an AppError at all — FastAPI's own
    request-schema validation — needed the same fix applied separately in
    main.py's handle_validation_error, since it never goes through
    AppError.to_payload(). A missing Idempotency-Key header (covered by the
    tests above) is actually a custom AppError, not this path — this needs a
    body FastAPI's own pydantic model rejects before the route ever runs, so
    `lines` is dropped entirely rather than just having the header removed.
    """
    token = issue_access_token(seed, role="customer")
    bad_body = {"address": _order_body()["address"], "payment_method": "cod"}
    resp = await client.post(
        "/v1/orders",
        json=bad_body,
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "bad-body-key-001"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["request_id"]
    assert body["error"]["request_id"] == resp.headers["X-Request-Id"]


async def test_request_id_header_is_present_and_well_formed_on_every_response(client):
    resp = await client.get("/v1/health/live")
    request_id = resp.headers.get("X-Request-Id")
    assert request_id is not None
    assert 8 <= len(request_id) <= 64


async def test_successful_request_log_line_carries_a_real_request_id_not_a_dash(client, seed):
    """AAD-OPS-003 regression, proven against the real formatter and the
    real contextvar timing rather than just reading the fixed source: a
    StreamHandler attached here formats synchronously, inside the same
    `log.info(...)` call the middleware makes — while `request_id_var` is
    still set to this request's id, before the middleware's own `finally`
    resets it. Before the fix, this same mechanism would have captured
    request_id: "-" for every successful request.
    """
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    access_log = logging.getLogger("app.access")
    previous_level = access_log.level
    access_log.addHandler(handler)
    access_log.setLevel(logging.INFO)
    try:
        token = issue_access_token(seed, role="customer")
        resp = await client.get(
            "/v1/orders", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200
    finally:
        access_log.removeHandler(handler)
        access_log.setLevel(previous_level)

    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    assert lines, "expected at least one access-log line to have been emitted"
    payloads = [json.loads(line) for line in lines]
    request_lines = [p for p in payloads if p.get("msg") == "request"]
    assert request_lines, f"no 'request' log line found among: {payloads}"
    assert request_lines[-1]["request_id"] != "-"
    assert request_lines[-1]["request_id"] == resp.headers["X-Request-Id"]


async def test_unhandled_exception_is_a_safe_500_with_no_traceback_leaked(
    client, seed, monkeypatch
):
    """The generic `handle_unexpected` exception handler is the very last
    safety net — a real, unexpected bug anywhere in a route must never
    reach the client as a raw traceback or an unhandled ASGI error."""
    from app.services.order_service import OrderService

    async def _boom(self, order_id, user_id):
        raise RuntimeError("simulated unexpected bug, deep inside the service layer")

    monkeypatch.setattr(OrderService, "get_for_user", _boom)

    token = issue_access_token(seed, role="customer")
    resp = await client.get(
        "/v1/orders/ord_whatever", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"]["code"] == "internal_error"
    assert "RuntimeError" not in json.dumps(body)
    assert "simulated unexpected bug" not in json.dumps(body)
    assert "request_id" in body["error"]


async def test_update_me_returns_the_newly_saved_address_in_the_same_response(client, seed):
    """AAD-PERF-003, at the HTTP boundary: `update_me` mutates the profile
    and the address in the same request, then re-reads the row for its
    response body. `tests/test_batch21_hygiene.py` pins the repository-level
    mechanism directly; this proves the real route wires `fresh=True` into
    the one call that actually needs it -- the response must carry the
    address just saved, not the stale pre-write snapshot the identity map
    would otherwise still be holding from `update_profile`'s own read.
    """
    token = issue_access_token(seed, role="customer")
    resp = await client.patch(
        "/v1/auth/me",
        json={
            "name": "New Name",
            "address": {
                "label": "Home", "line1": "1 Farm Road", "city": "Hyderabad",
                "pincode": "500001",
            },
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "New Name"
    assert len(body["addresses"]) == 1
    assert body["addresses"][0]["label"] == "Home"
    assert body["addresses"][0]["line1"] == "1 Farm Road"
