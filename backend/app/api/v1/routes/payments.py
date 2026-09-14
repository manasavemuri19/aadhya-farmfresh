from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response, status
from fastapi.responses import RedirectResponse
from urllib.parse import urlencode

from app.api.deps import CurrentUser, get_order_repo, get_order_service
from app.api.route import TransactionalRoute
from app.core.config import settings
from app.core.errors import NotFound, PaymentFailed, UpstreamError, ValidationError
from app.payments import PaymentProvider, get_payment_provider
from app.repositories.orders import OrderRepository
from app.schemas.order import OrderView
from app.services.order_service import OrderService

log = logging.getLogger(__name__)
router = APIRouter(prefix="/payments", tags=["payments"], route_class=TransactionalRoute)

Orders = Annotated[OrderService, Depends(get_order_service)]
OrderRepo = Annotated[OrderRepository, Depends(get_order_repo)]
Payments = Annotated[PaymentProvider, Depends(get_payment_provider)]

# AAD-PAY-011: `/payments/verify` and `verify_checkout_signature` used to
# live here. They implemented Razorpay's *Standard Checkout* signature
# formula (`HMAC(secret, "order_id|payment_id")`) against a Payment Links
# integration, whose actual callback signature is the differently-shaped,
# already-correct `verify_payment_link_callback` below — so every real
# request to this route rejected with a false "checkout signature rejected"
# warning. It had no role once Payment Links became the integration (the
# mobile app never called it — see `checkout.tsx`), and `/link-callback`
# already does this job with the right formula, so it was deleted rather
# than fixed. If Standard Checkout is ever adopted, bring it back then.


@router.post("/webhook", status_code=status.HTTP_204_NO_CONTENT)
async def webhook(
    request: Request,
    svc: Orders,
    orders: OrderRepo,
    payments: Payments,
    x_razorpay_signature: Annotated[str | None, Header()] = None,
    x_mock_signature: Annotated[str | None, Header()] = None,
    x_razorpay_event_id: Annotated[str | None, Header()] = None,
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
        # AAD-PAY-009: Razorpay's own event id arrives in this header, not
        # anywhere in the body — read here and handed to parse_webhook so
        # the replay guard keys on the gateway's own id.
        event = payments.parse_webhook(
            body=raw, signature=signature, event_id=x_razorpay_event_id
        )
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


# AAD-SEC-023: the five parameters Razorpay's Payment Links redirect
# actually sends — see verify_payment_link_callback's signature and
# link_redirect below. Anything else arriving on this unauthenticated route
# is dropped rather than forwarded into the app's deep link.
_REDIRECT_PARAMS = (
    "razorpay_payment_id",
    "razorpay_payment_link_id",
    "razorpay_payment_link_reference_id",
    "razorpay_payment_link_status",
    "razorpay_signature",
)
_REDIRECT_PARAM_MAX_LEN = 200  # generous for a signature/id/status; not unbounded


@router.get("/link-redirect", include_in_schema=False)
async def link_redirect(request: Request) -> RedirectResponse:
    """The actual `callback_url` given to Razorpay's Payment Links API.

    Razorpay's Payment Links validate `callback_url` as a real `https://`
    address and reject a custom app scheme outright — confirmed against
    the live API ("callback_url: URL should be sent in callback_url
    field"), not assumed. So Razorpay redirects the phone's browser here
    first, and this single hop does nothing except immediately bounce it
    into the app's real destination, `aadhya://payment-callback`. The app
    itself never talks to this route directly — it only ever sees the
    `aadhya://` redirect this produces.

    AAD-SEC-023: this used to forward **every** query parameter unchanged —
    unauthenticated, unthrottled, on this app's own production domain, into
    a fixed-scheme deep link. Not a classic open redirect (the target
    scheme and path are fixed), but a reflector that could launch the app's
    payment-callback screen with attacker-chosen parameters from a URL that
    looks entirely legitimate because the domain is real. Forged
    confirmations still fail the HMAC check downstream (`AAD-PAY-007`), so
    money was never actually at risk — this closes the reflector itself:
    only the five parameters Razorpay documents are ever forwarded, each
    length-capped, and a request missing the signature outright is rejected
    here rather than handed to the app to sort out.
    """
    params = request.query_params
    if not params.get("razorpay_signature"):
        raise ValidationError("This payment redirect is missing its signature.")

    forwarded = {
        key: value
        for key, value in params.items()
        if key in _REDIRECT_PARAMS and len(value) <= _REDIRECT_PARAM_MAX_LEN
    }
    query = urlencode(forwarded)
    return RedirectResponse(url=f"aadhya://payment-callback?{query}", status_code=302)


@router.get("/link-callback", include_in_schema=False)
async def payment_link_callback(
    svc: Orders,
    orders: OrderRepo,
    payments: Payments,
    razorpay_payment_id: str,
    razorpay_payment_link_id: str,
    razorpay_payment_link_reference_id: str,
    razorpay_payment_link_status: str,
    razorpay_signature: str,
) -> dict[str, str]:
    """The app's own deep link opens here after the customer pays on
    Razorpay's hosted Payment Link page and gets redirected back — see
    `RazorpayProvider.create_order` for why Payment Links rather than the
    Orders API.

    This route is UX only (AAD-PAY-006): it exists so the app can show a
    success screen without waiting on the webhook, not because it is
    required to confirm anything. The premise this route used to operate
    on — that Payment Links have no separate webhook contract to lean on —
    was wrong; Razorpay publishes `payment_link.paid` and related events,
    and `RazorpayProvider.parse_webhook` now handles them (AAD-PAY-007).
    That webhook is the authoritative confirmation and does not depend on
    this route ever being reached at all.

    Because this is UX only, it does not require the customer to still be
    authenticated: the HMAC signature *is* the authentication for this
    request, and a 30-minute access token expiring while the customer was
    on Razorpay's hosted page must never be the reason a paid order is
    never shown as confirmed (see the former AAD-PAY-012). The app fetches
    the full order separately, with its own token, after this returns.

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

    # Only "paid" moves anything. A failed/expired/cancelled status here is
    # UX signal for this one customer's screen, not an authoritative outcome
    # for the order — the webhook (or the hold-expiry poll-and-reconcile
    # backstop) is what actually fails or cancels it, so this route never
    # takes a state-changing action on anything but a genuine payment.
    if razorpay_payment_link_status == "paid":
        # AAD-PAY-010: this used to build a WebhookEvent by hand with
        # amount_paise=None — apply_webhook's amount-mismatch check only
        # runs when an amount is present, so that silently switched off the
        # one control standing between an underpaid Payment Link (partial
        # payment is not explicitly disabled at link creation, AAD-PAY-014)
        # and this route confirming it anyway. `poll_status` makes a real
        # fetch of the link from Razorpay and returns the amount it
        # actually reports paid, reusing the exact mechanism the
        # poll-and-reconcile sweep already relies on for the same reason.
        try:
            event = await payments.poll_status(provider_order_id=razorpay_payment_link_id)
        except UpstreamError:
            # Never fatal here: this route is UX only (see the docstring
            # above) — the webhook, or the next sweep, confirms the order
            # regardless of whether this one live-fetch succeeds.
            log.warning(
                "link-callback could not reach the gateway to confirm the amount; "
                "the webhook will confirm this order instead",
                extra={"order": order_id},
            )
            event = None
        if event is not None:
            first_time = await orders.record_webhook_once(payments.name, event.event_id, event.raw)
            if first_time:
                await svc.apply_webhook(event)

    return {"order_id": order_id, "status": razorpay_payment_link_status}