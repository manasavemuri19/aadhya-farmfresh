"""Real HTTP-level coverage for AAD-OPS-015.

`test_razorpay_payment_link.py`'s own docstring used to claim it proves
"the exact cryptographic formula **and endpoint wiring** are correct" — but
every test in that file calls `provider.verify_payment_link_callback(...)`
directly. Nothing there ever sends a request. `test_payment_link_confirmation.py`
goes one layer further and exercises `parse_webhook` → `apply_webhook`, but
also only at the service layer — it hand-builds a `WebhookEvent` and calls
`order_service.apply_webhook(event)` itself, never through a route.

Neither file proves what a real gateway request does when it lands on this
app: raw-body HMAC verification in the `POST /payments/webhook` handler
itself, the `record_webhook_once` replay guard wired to that route, or the
`GET /payments/link-callback` handler that verifies its own (differently
formulated) signature and only conditionally applies a webhook event. This
file drives real ASGI requests at both routes, through the actual FastAPI
dependency graph (`get_payment_provider()` resolving a real `RazorpayProvider`,
the real registered exception handlers), following the ASGI pattern already
established by `test_idempotency_key_header.py`.

`get_payment_provider()` is `@lru_cache(maxsize=1)` — a process-lifetime
singleton. Tests that need requests to resolve a real `RazorpayProvider`
(instead of whatever the process already cached, normally `MockPaymentProvider`)
must clear that cache after pointing `settings.payment_provider` at
"razorpay", and clear it again afterwards so later tests get their own
provider back.
"""

from __future__ import annotations

import hashlib
import hmac
import json
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
from app.domain.enums import OrderStatus, PaymentMethod, PaymentStatus  # noqa: E402
from app.payments import get_payment_provider  # noqa: E402
from app.payments.mock import MockPaymentProvider  # noqa: E402
from app.repositories.products import ProductRepository  # noqa: E402
from app.schemas.catalog import Product, Variant  # noqa: E402

TEST_DATABASE_URL = os.environ["DATABASE_URL"]
SKU = "MILK-COW-1L-PAYEP"

FAKE_KEY_SECRET = "fake-key-secret-for-endpoint-verification-only"
FAKE_WEBHOOK_SECRET = "fake-webhook-secret-for-endpoint-verification-only"


def _webhook_body(payload: dict) -> tuple[bytes, str]:
    body = json.dumps(payload).encode()
    signature = hmac.new(FAKE_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return body, signature


def _callback_signature(link_id: str, reference_id: str, status: str, payment_id: str) -> str:
    message = f"{link_id}|{reference_id}|{status}|{payment_id}".encode()
    return hmac.new(FAKE_KEY_SECRET.encode(), message, hashlib.sha256).hexdigest()


@pytest.fixture(autouse=True)
def razorpay_provider(monkeypatch):
    """Point real DI at a real `RazorpayProvider` with fake credentials —
    these tests are exercising the endpoint's signature verification and
    replay handling, not a live Razorpay account."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "payment_provider", "razorpay")
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_fake")
    monkeypatch.setattr(settings, "razorpay_key_secret", FAKE_KEY_SECRET)
    monkeypatch.setattr(settings, "razorpay_webhook_secret", FAKE_WEBHOOK_SECRET)
    get_payment_provider.cache_clear()
    yield
    get_payment_provider.cache_clear()


@pytest.fixture
async def seeded():
    """A real user, a buyable variant, and a real PENDING_PAYMENT order with
    a known `provider_order_id` — created through `OrderService.create_order`
    with `MockPaymentProvider` (fast, offline) rather than hand-built, so the
    order/lines/payment rows are exactly what checkout actually produces.
    The provider_order_id it generates is opaque and provider-agnostic, so
    reusing it as the Payment Link id in these tests' payloads is realistic:
    `apply_webhook` never inspects which provider created the order, only
    the id and (for payment_link events) `reference_id`.
    """
    from app.repositories.idempotency import IdempotencyRepository
    from app.repositories.orders import OrderRepository
    from app.schemas.auth import Address
    from app.schemas.order import CartLineInput, CreateOrderRequest
    from app.services.order_service import OrderService

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
            slug="full-cream-cow-milk-payep",
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
                id=user_id, google_sub="payep_test_sub",
                email="payep@example.com", name="Payep Test",
            )
        )
        await session.commit()

    async with factory() as session:
        orders = OrderRepository(session)
        svc = OrderService(
            ProductRepository(session), orders, IdempotencyRepository(session),
            MockPaymentProvider(),
        )
        order = await svc.create_order(
            user_id=user_id,
            request=CreateOrderRequest(
                lines=[CartLineInput(sku=SKU, qty=1)],
                address=Address(
                    label="Home", line1="12 Farm Road", city="Hyderabad", pincode="500001",
                ),
                payment_method=PaymentMethod.ONLINE,
            ),
            idempotency_key=None,
        )
        await session.commit()
        doc = await orders.get(order.id)

    yield {
        "user_id": user_id,
        "order_id": order.id,
        "total_paise": doc["total_paise"],
        "provider_order_id": doc["payment"]["provider_order_id"],
    }
    await engine.dispose()


@pytest.fixture
async def asgi_client(seeded):
    from app.main import app as real_app

    token = issue_access_token(seeded["user_id"], role="customer")
    async with real_app.router.lifespan_context(real_app):
        transport = httpx.ASGITransport(app=real_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            client.headers["Authorization"] = f"Bearer {token}"
            yield client


async def _order_status(seeded) -> tuple[str, str]:
    """Re-reads the order fresh over its own connection, independent of the
    ASGI request's own transaction/session."""
    from app.repositories.orders import OrderRepository

    engine = create_async_engine(TEST_DATABASE_URL, poolclass=None)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        doc = await OrderRepository(session).get(seeded["order_id"])
    await engine.dispose()
    assert doc is not None
    return doc["status"], doc["payment"]["status"]


def _paid_webhook_payload(seeded, *, payment_id: str = "pay_endpoint_test") -> dict:
    return {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": seeded["provider_order_id"],
                    "reference_id": seeded["order_id"],
                    "amount_paid": seeded["total_paise"],
                }
            },
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": "order_unrelated_internal_link_order",
                    "amount": seeded["total_paise"],
                }
            },
        },
    }


# ---------------------------------------------------------------------------
# POST /v1/payments/webhook
# ---------------------------------------------------------------------------


async def test_signed_webhook_confirms_the_order(asgi_client, seeded):
    body, signature = _webhook_body(_paid_webhook_payload(seeded))

    resp = await asgi_client.post(
        "/v1/payments/webhook", content=body,
        headers={"content-type": "application/json", "x-razorpay-signature": signature},
    )

    assert resp.status_code == 204, resp.text
    status_, payment_status = await _order_status(seeded)
    assert status_ == OrderStatus.CONFIRMED.value
    assert payment_status == PaymentStatus.CAPTURED.value


async def test_replaying_the_same_webhook_event_is_still_204_and_a_no_op(asgi_client, seeded):
    body, signature = _webhook_body(_paid_webhook_payload(seeded))
    headers = {"content-type": "application/json", "x-razorpay-signature": signature}

    first = await asgi_client.post("/v1/payments/webhook", content=body, headers=headers)
    second = await asgi_client.post("/v1/payments/webhook", content=body, headers=headers)

    assert first.status_code == 204, first.text
    assert second.status_code == 204, second.text
    status_, payment_status = await _order_status(seeded)
    assert status_ == OrderStatus.CONFIRMED.value
    assert payment_status == PaymentStatus.CAPTURED.value


async def _webhook_event_rows(seeded) -> list[str]:
    """Reads the replay-guard table directly, independent of the ASGI
    request's own transaction — proves what `record_webhook_once` actually
    keyed each row on, rather than inferring it from order state."""
    from sqlalchemy import select

    from app.db.models import WebhookEvent as WebhookEventRow

    engine = create_async_engine(TEST_DATABASE_URL, poolclass=None)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        rows = (
            await session.execute(
                select(WebhookEventRow.event_id).where(WebhookEventRow.provider == "razorpay")
            )
        ).scalars().all()
    await engine.dispose()
    return list(rows)


async def test_webhook_uses_the_event_id_header_not_a_body_hash_for_replay_dedup(
    asgi_client, seeded
):
    """AAD-PAY-009: before the fix, the replay guard keyed on a hash of the
    body. Two *distinct* gateway deliveries that happen to carry identical
    bodies (Razorpay's own retries of two different events, or two events
    whose payload is genuinely the same) would then collapse into one. The
    fix reads Razorpay's own `x-razorpay-event-id` header and keys on that
    instead — so the same body with two different event ids must record as
    two separate rows, not one."""
    body, signature = _webhook_body(_paid_webhook_payload(seeded))
    headers_common = {"content-type": "application/json", "x-razorpay-signature": signature}

    first = await asgi_client.post(
        "/v1/payments/webhook", content=body,
        headers={**headers_common, "x-razorpay-event-id": "evt_aad_pay_009_a"},
    )
    second = await asgi_client.post(
        "/v1/payments/webhook", content=body,
        headers={**headers_common, "x-razorpay-event-id": "evt_aad_pay_009_b"},
    )

    assert first.status_code == 204, first.text
    assert second.status_code == 204, second.text
    event_ids = await _webhook_event_rows(seeded)
    assert sorted(event_ids) == ["evt_aad_pay_009_a", "evt_aad_pay_009_b"]


async def test_webhook_treats_the_same_event_id_header_as_a_duplicate_regardless_of_body(
    asgi_client, seeded
):
    """The other direction: two *different* bodies delivered under the same
    `x-razorpay-event-id` (a gateway retry that re-signs, or a delivery
    Razorpay itself considers one logical event) must still dedup as one
    row, keyed on the header — not fall through to the old body-hash
    fallback just because the bytes differ."""
    body_a, sig_a = _webhook_body(_paid_webhook_payload(seeded, payment_id="pay_aad_pay_009_x"))
    body_b, sig_b = _webhook_body(_paid_webhook_payload(seeded, payment_id="pay_aad_pay_009_y"))

    first = await asgi_client.post(
        "/v1/payments/webhook", content=body_a,
        headers={
            "content-type": "application/json",
            "x-razorpay-signature": sig_a,
            "x-razorpay-event-id": "evt_aad_pay_009_dup",
        },
    )
    second = await asgi_client.post(
        "/v1/payments/webhook", content=body_b,
        headers={
            "content-type": "application/json",
            "x-razorpay-signature": sig_b,
            "x-razorpay-event-id": "evt_aad_pay_009_dup",
        },
    )

    assert first.status_code == 204, first.text
    assert second.status_code == 204, second.text
    event_ids = await _webhook_event_rows(seeded)
    assert event_ids == ["evt_aad_pay_009_dup"]


async def test_webhook_with_a_bad_signature_is_rejected_and_does_not_confirm(asgi_client, seeded):
    body, _ = _webhook_body(_paid_webhook_payload(seeded))

    resp = await asgi_client.post(
        "/v1/payments/webhook", content=body,
        headers={"content-type": "application/json", "x-razorpay-signature": "0" * 64},
    )

    assert resp.status_code == 402, resp.text
    assert resp.json()["error"]["code"] == "payment_failed"
    status_, payment_status = await _order_status(seeded)
    assert status_ == OrderStatus.PENDING_PAYMENT.value
    assert payment_status == PaymentStatus.CREATED.value


# ---------------------------------------------------------------------------
# GET /v1/payments/link-redirect
# ---------------------------------------------------------------------------


async def test_link_redirect_forwards_only_the_documented_params(asgi_client):
    """AAD-SEC-023: before the fix, every query parameter on this
    unauthenticated, production-domain route was forwarded unchanged into
    the app's `aadhya://` deep link. This proves only the five parameters
    Razorpay actually sends survive, and an attacker-chosen extra param
    (here, `redirect_to`, shaped like something a phishing attempt might
    add) is dropped rather than reaching the app."""
    from urllib.parse import parse_qs, urlparse

    resp = await asgi_client.get(
        "/v1/payments/link-redirect",
        params={
            "razorpay_payment_id": "pay_sec023",
            "razorpay_payment_link_id": "plink_sec023",
            "razorpay_payment_link_reference_id": "ord_sec023",
            "razorpay_payment_link_status": "paid",
            "razorpay_signature": "deadbeef",
            "redirect_to": "https://not-razorpay.example/steal",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 302, resp.text
    location = resp.headers["location"]
    assert location.startswith("aadhya://payment-callback?")
    forwarded = parse_qs(urlparse(location).query)
    assert set(forwarded) == {
        "razorpay_payment_id",
        "razorpay_payment_link_id",
        "razorpay_payment_link_reference_id",
        "razorpay_payment_link_status",
        "razorpay_signature",
    }
    assert forwarded["razorpay_signature"] == ["deadbeef"]


async def test_link_redirect_without_a_signature_is_rejected(asgi_client):
    resp = await asgi_client.get(
        "/v1/payments/link-redirect",
        params={"razorpay_payment_link_status": "paid"},
        follow_redirects=False,
    )

    assert resp.status_code == 422, resp.text


async def test_link_redirect_drops_an_oversized_param_value(asgi_client):
    """A length cap on top of the allowlist — even one of the five real
    param names is dropped if its value is implausibly long."""
    from urllib.parse import parse_qs, urlparse

    resp = await asgi_client.get(
        "/v1/payments/link-redirect",
        params={
            "razorpay_signature": "deadbeef",
            "razorpay_payment_id": "x" * 201,
        },
        follow_redirects=False,
    )

    assert resp.status_code == 302, resp.text
    forwarded = parse_qs(urlparse(resp.headers["location"]).query)
    assert "razorpay_payment_id" not in forwarded
    assert forwarded["razorpay_signature"] == ["deadbeef"]


# ---------------------------------------------------------------------------
# GET /v1/payments/link-callback
# ---------------------------------------------------------------------------


def _mock_link_fetch(seeded, *, amount_paid: int, status: str = "paid"):
    """Stands in for the real `razorpay.Client.payment_link.fetch(...)` SDK
    call `RazorpayProvider.poll_status` makes (AAD-PAY-010) — no live
    Razorpay account exists to fetch from, so this returns exactly the
    shape that call returns."""

    def fetch(provider_order_id: str) -> dict:
        return {
            "id": seeded["provider_order_id"],
            "status": status,
            "amount_paid": amount_paid,
            "reference_id": seeded["order_id"],
            "payments": [{"payment_id": "pay_callback_test"}],
        }

    return fetch


async def test_signed_paid_callback_confirms_the_order(asgi_client, seeded, monkeypatch):
    from app.payments import get_payment_provider

    signature = _callback_signature(
        seeded["provider_order_id"], seeded["order_id"], "paid", "pay_callback_test",
    )

    # AAD-PAY-010: the callback route now fetches the live Payment Link from
    # Razorpay to get the actually-captured amount, rather than skipping
    # the amount check entirely — so this route's own confirmation now
    # depends on that fetch, which has no live account to reach.
    provider = get_payment_provider()
    monkeypatch.setattr(
        provider._client.payment_link, "fetch",
        _mock_link_fetch(seeded, amount_paid=seeded["total_paise"]),
    )

    resp = await asgi_client.get(
        "/v1/payments/link-callback",
        params={
            "razorpay_payment_id": "pay_callback_test",
            "razorpay_payment_link_id": seeded["provider_order_id"],
            "razorpay_payment_link_reference_id": seeded["order_id"],
            "razorpay_payment_link_status": "paid",
            "razorpay_signature": signature,
        },
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"order_id": seeded["order_id"], "status": "paid"}
    status_, payment_status = await _order_status(seeded)
    assert status_ == OrderStatus.CONFIRMED.value
    assert payment_status == PaymentStatus.CAPTURED.value


async def test_signed_paid_callback_does_not_confirm_when_the_live_amount_is_short(
    asgi_client, seeded, monkeypatch
):
    """AAD-PAY-010's regression case: the query string says "paid" (and is
    correctly signed — this is not a forgery), but Razorpay's own live
    record of the link shows less was actually captured than the order
    costs. Before this fix, the callback built its own WebhookEvent with
    amount_paise=None, which skips apply_webhook's amount-mismatch check
    entirely — this proves that check is now actually wired up."""
    from app.payments import get_payment_provider

    signature = _callback_signature(
        seeded["provider_order_id"], seeded["order_id"], "paid", "pay_callback_test",
    )

    provider = get_payment_provider()
    monkeypatch.setattr(
        provider._client.payment_link, "fetch",
        _mock_link_fetch(seeded, amount_paid=seeded["total_paise"] - 100),
    )

    resp = await asgi_client.get(
        "/v1/payments/link-callback",
        params={
            "razorpay_payment_id": "pay_callback_test",
            "razorpay_payment_link_id": seeded["provider_order_id"],
            "razorpay_payment_link_reference_id": seeded["order_id"],
            "razorpay_payment_link_status": "paid",
            "razorpay_signature": signature,
        },
    )

    # Still 200 — the signature is genuinely valid, this is not a forged
    # request, and the route's own contract doesn't change. It just must
    # not have confirmed the order.
    assert resp.status_code == 200, resp.text
    status_, payment_status = await _order_status(seeded)
    assert status_ == OrderStatus.PENDING_PAYMENT.value
    # AAD-PAY-005: no longer left at CREATED with the money stranded — the
    # short amount is flagged so the sweep refunds it. See
    # tests/test_amount_mismatch.py for that behaviour in full.
    assert payment_status == PaymentStatus.AMOUNT_MISMATCH.value


async def test_callback_with_a_bad_signature_is_rejected_and_does_not_confirm(asgi_client, seeded):
    resp = await asgi_client.get(
        "/v1/payments/link-callback",
        params={
            "razorpay_payment_id": "pay_callback_test",
            "razorpay_payment_link_id": seeded["provider_order_id"],
            "razorpay_payment_link_reference_id": seeded["order_id"],
            "razorpay_payment_link_status": "paid",
            "razorpay_signature": "0" * 64,
        },
    )

    assert resp.status_code == 402, resp.text
    assert resp.json()["error"]["code"] == "payment_failed"
    status_, payment_status = await _order_status(seeded)
    assert status_ == OrderStatus.PENDING_PAYMENT.value
    assert payment_status == PaymentStatus.CREATED.value


async def test_callback_with_a_non_paid_status_is_accepted_but_does_not_confirm(
    asgi_client, seeded
):
    """A correctly signed callback for e.g. "cancelled" is not a forged
    request — it must still 200 — but only "paid" moves the order, since
    that's the only status the callback route turns into a webhook event."""
    signature = _callback_signature(
        seeded["provider_order_id"], seeded["order_id"], "cancelled", "pay_callback_test",
    )

    resp = await asgi_client.get(
        "/v1/payments/link-callback",
        params={
            "razorpay_payment_id": "pay_callback_test",
            "razorpay_payment_link_id": seeded["provider_order_id"],
            "razorpay_payment_link_reference_id": seeded["order_id"],
            "razorpay_payment_link_status": "cancelled",
            "razorpay_signature": signature,
        },
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"order_id": seeded["order_id"], "status": "cancelled"}
    status_, payment_status = await _order_status(seeded)
    assert status_ == OrderStatus.PENDING_PAYMENT.value
    assert payment_status == PaymentStatus.CREATED.value
