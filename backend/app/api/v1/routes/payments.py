from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response, status

from app.api.deps import CurrentUser, get_order_repo, get_order_service
from app.core.config import settings
from app.core.errors import NotFound, PaymentFailed
from app.payments import PaymentProvider, get_payment_provider
from app.repositories.orders import OrderRepository
from app.schemas.order import OrderView, VerifyPaymentRequest
from app.services.order_service import OrderService

log = logging.getLogger(__name__)
router = APIRouter(prefix="/payments", tags=["payments"])

Orders = Annotated[OrderService, Depends(get_order_service)]
OrderRepo = Annotated[OrderRepository, Depends(get_order_repo)]
Payments = Annotated[PaymentProvider, Depends(get_payment_provider)]


@router.post("/verify", response_model=OrderView)
async def verify_payment(
    body: VerifyPaymentRequest, principal: CurrentUser, svc: Orders, payments: Payments
) -> OrderView:
    """Client-side confirmation after the gateway sheet closes.

    This exists so the app can show a success screen without waiting on the
    webhook. It verifies the signature but does not itself confirm the order —
    the webhook does that. If the webhook is slow, the client simply polls.
    """
    ok = payments.verify_checkout_signature(
        provider_order_id=body.provider_order_id,
        provider_payment_id=body.provider_payment_id,
        signature=body.signature,
    )
    if not ok:
        log.warning("checkout signature rejected", extra={"order": body.order_id})
        raise PaymentFailed("We could not verify that payment.")
    return await svc.get_for_user(body.order_id, principal.user_id)


@router.post("/webhook", status_code=status.HTTP_204_NO_CONTENT)
async def webhook(
    request: Request,
    svc: Orders,
    orders: OrderRepo,
    payments: Payments,
    x_razorpay_signature: Annotated[str | None, Header()] = None,
    x_mock_signature: Annotated[str | None, Header()] = None,
) -> Response:
    """Gateway callback — the authoritative record of payment.

    Three guarantees, in order: the signature is verified against the *raw*
    body before anything is parsed; the event id is recorded so replays become
    no-ops; and a 2xx is returned even for events we ignore, so the gateway
    stops retrying instead of hammering us.
    """
    raw = await request.body()
    signature = x_razorpay_signature or x_mock_signature or ""

    try:
        event = payments.parse_webhook(body=raw, signature=signature)
    except PaymentFailed:
        # Signature failure is the one case worth a 4xx: it is either an attack
        # or a misconfigured secret, and both need to be visible.
        raise

    first_time = await orders.record_webhook_once(payments.name, event.event_id, event.raw)
    if not first_time:
        log.info("duplicate webhook ignored", extra={"event_id": event.event_id})
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    await svc.apply_webhook(event)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/mock/sign", include_in_schema=False)
async def mock_sign(
    provider_order_id: str, provider_payment_id: str, payments: Payments
) -> dict[str, str]:
    """Local-only helper so the checkout flow can be driven end to end without
    a gateway account. Absent whenever the real provider is configured."""
    if settings.payment_provider != "mock":
        raise NotFound("Not available.")
    from app.payments.mock import MockPaymentProvider

    assert isinstance(payments, MockPaymentProvider)
    return {
        "signature": payments.sign_for_testing(provider_order_id, provider_payment_id)
    }


@router.post("/mock/complete", response_model=OrderView, include_in_schema=False)
async def mock_complete_payment(
    body: dict, principal: CurrentUser, svc: Orders, orders: OrderRepo, payments: Payments
) -> OrderView:
    """Local-only stand-in for the gateway's webhook.

    `/payments/verify` only checks a signature for the client's own UX — the
    real confirmation always comes from the gateway's webhook, delivered
    asynchronously from Razorpay's own servers, never from the client. A mock
    provider has no such courier, so this endpoint exists purely to produce
    the same effect a real webhook delivery would: it builds the identical
    `WebhookEvent` the real `/payments/webhook` route would receive and runs
    it through the exact same `apply_webhook` path, rather than reimplementing
    order-confirmation logic a second time.

    Absent whenever a real provider is configured — this is not a route a
    production build ever exposes.
    """
    if settings.payment_provider != "mock":
        raise NotFound("Not available.")
    from app.payments.base import WebhookEvent
    from app.core.ids import new_id

    order_id = body.get("order_id")
    outcome = body.get("outcome", "success")
    if not order_id:
        raise NotFound("order_id is required.")

    order = await svc.get_for_user(order_id, principal.user_id)
    provider_order_id = order.payment.provider_order_id
    if not provider_order_id:
        raise NotFound("This order has no payment session.")

    event = WebhookEvent(
        event_id=new_id("mockevt", 12),
        event_type="payment.captured" if outcome == "success" else "payment.failed",
        provider_order_id=provider_order_id,
        provider_payment_id=f"mockpay_{new_id('', 10)}",
        amount_paise=order.total_paise,
        raw={"source": "mock_complete_endpoint"},
    )

    first_time = await orders.record_webhook_once(payments.name, event.event_id, event.raw)
    if first_time:
        await svc.apply_webhook(event)

    return await svc.get_for_user(order_id, principal.user_id)


@router.get("/link-callback", response_model=OrderView)
async def payment_link_callback(
    principal: CurrentUser,
    svc: Orders,
    orders: OrderRepo,
    payments: Payments,
    razorpay_payment_id: str,
    razorpay_payment_link_id: str,
    razorpay_payment_link_reference_id: str,
    razorpay_payment_link_status: str,
    razorpay_signature: str,
) -> OrderView:
    """The app's own deep link opens here after the customer pays on
    Razorpay's hosted Payment Link page and gets redirected back — see
    `RazorpayProvider.create_order` for why Payment Links rather than the
    Orders API. Razorpay's own documentation for this flow says to verify
    the signature and then mark the order as paid directly from that; unlike
    Standard Checkout's callback_url (which they explicitly warn is UX-only,
    with the async webhook as the real source of truth), Payment Links do
    not have an equivalent separate webhook contract for this app to lean
    on instead, so the verified redirect *is* the confirmation here.

    Only reachable with the real Razorpay provider configured — the mock
    provider never produces a payment link, so this route has nothing to
    verify against in mock mode.
    """
    if settings.payment_provider != "razorpay":
        raise NotFound("Not available.")
    from app.payments.razorpay import RazorpayProvider

    assert isinstance(payments, RazorpayProvider)
    ok = payments.verify_payment_link_callback(
        payment_link_id=razorpay_payment_link_id,
        payment_link_reference_id=razorpay_payment_link_reference_id,
        payment_link_status=razorpay_payment_link_status,
        payment_id=razorpay_payment_id,
        signature=razorpay_signature,
    )
    if not ok:
        log.warning(
            "payment link callback signature rejected",
            extra={"reference_id": razorpay_payment_link_reference_id},
        )
        raise PaymentFailed("We could not verify that payment.")

    order_id = razorpay_payment_link_reference_id  # set to our order id at link creation

    from app.payments.base import WebhookEvent

    event = WebhookEvent(
        event_id=f"linkcb_{razorpay_payment_id}",
        event_type=(
            "payment.captured" if razorpay_payment_link_status == "paid" else "payment.failed"
        ),
        provider_order_id=razorpay_payment_link_id,
        provider_payment_id=razorpay_payment_id,
        amount_paise=None,  # webhook path checks this only when present
        raw={
            "source": "link_callback",
            "status": razorpay_payment_link_status,
        },
    )

    first_time = await orders.record_webhook_once(payments.name, event.event_id, event.raw)
    if first_time:
        await svc.apply_webhook(event)

    return await svc.get_for_user(order_id, principal.user_id)