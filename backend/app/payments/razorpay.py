"""Razorpay implementation of the payment contract.

Two signature schemes are involved and they are not the same thing:

  * the *Payment Link callback* signature is HMAC over the four-field
    `payment_link_id|reference_id|status|payment_id`, verified in
    `verify_payment_link_callback` below;
  * the *webhook* signature is HMAC over the raw request body using a separate
    webhook secret configured in the Razorpay dashboard.

(A third, *Standard Checkout* formula — HMAC over "order_id|payment_id" —
used to live here too, backing `/payments/verify`. It was deleted in
AAD-PAY-011: this integration uses Payment Links, not Standard Checkout, so
that formula never matched anything a real client sent.)

Both remaining schemes are verified with `hmac.compare_digest`. The webhook is
authoritative for money; the callback signature only lets the app show a
success screen sooner.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from datetime import datetime
from typing import Any

import razorpay
import requests
from razorpay.errors import BadRequestError

from app.core.config import settings
from app.core.errors import PaymentFailed, UpstreamError
from app.payments.base import PaymentProvider, ProviderOrder, WebhookEvent

log = logging.getLogger(__name__)


class _TimeoutSession(requests.Session):
    """A `requests.Session` with a real default timeout (AAD-PAY-008).

    `requests` has no built-in way to set a timeout at the session level —
    this override is the standard workaround, applying `timeout` to any
    request that doesn't already specify its own. It exists because
    `razorpay.Client` builds a plain `requests.Session()` internally with no
    timeout at all, so a hung TCP connection would otherwise wait forever —
    and every `self.session.get/post/...` call the SDK makes routes through
    `Session.request()` underneath, so overriding just that one method here
    covers the whole client, including any call site added to it later.
    """

    def __init__(self, timeout: float) -> None:
        super().__init__()
        self._default_timeout = timeout

    def request(self, *args: Any, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self._default_timeout)
        return super().request(*args, **kwargs)


class RazorpayProvider(PaymentProvider):
    name = "razorpay"

    # The `razorpay` SDK wraps `requests`, which is synchronous and, without
    # this, has no timeout at all (`None` means wait forever). A hung
    # connection to Razorpay would otherwise block this worker's event loop
    # first (nothing else it's serving gets scheduled) and then, once moved
    # off the loop below, tie up a thread-pool thread indefinitely instead —
    # this bound stops both. 10s comfortably covers the 300ms-2s round
    # trips this audit measured from India, with real room for a genuine
    # slow patch, and one value for every call keeps the client's timeout
    # behaviour uniform rather than something to remember per call site.
    _TIMEOUT_SECONDS = 10.0

    def __init__(self) -> None:
        if not (settings.razorpay_key_id and settings.razorpay_key_secret):
            raise RuntimeError("Razorpay credentials are not configured")
        # AAD-SEC-008: razorpay_key_secret/razorpay_webhook_secret are
        # SecretStr now — .get_secret_value() unwraps to the actual string
        # only at the point of use, so nothing else in the process (a repr,
        # a traceback, an accidental log) ever sees the plaintext value.
        self._client = razorpay.Client(
            session=_TimeoutSession(self._TIMEOUT_SECONDS),
            auth=(settings.razorpay_key_id, settings.razorpay_key_secret.get_secret_value()),
        )
        self._secret = settings.razorpay_key_secret.get_secret_value().encode()
        self._webhook_secret = settings.razorpay_webhook_secret.get_secret_value().encode()

    async def create_order(
        self,
        *,
        amount_paise: int,
        currency: str,
        receipt: str,
        notes: dict[str, str],
        expires_at: datetime | None = None,
    ) -> ProviderOrder:
        """Creates a Razorpay **Payment Link**, not an Orders-API order.

        This is a deliberate choice, not the simpler/default path: Payment
        Links are the mechanism Razorpay documents for exactly this shape of
        flow — send the customer to a hosted page, they pay there, they're
        redirected back via `callback_url`. The alternative (Standard
        Checkout's in-app popup) requires the native `react-native-razorpay`
        SDK, which means a new compiled build every time it changes. A
        Payment Link is just a URL — opening it needs nothing beyond what
        the app already ships with, so this whole integration can ship and
        iterate over `eas update`.

        `reference_id` is set to our own order id, so the redirect callback
        (and any later lookup) can find the order without needing to persist
        a separate mapping.

        `expires_at`, when given, becomes the link's own `expire_by` — the
        same instant the order's payment hold expires — so the app and
        Razorpay cannot disagree about when a payment is dead (AAD-PAY-006 /
        AAD-PAY-014). Partial payment is disabled explicitly rather than
        left to the default, since nothing downstream is built to reconcile
        a payment that only covers part of the order.
        """
        body: dict[str, Any] = {
            "amount": amount_paise,
            "currency": currency,
            "description": "Aadya Dairy order",
            "reference_id": receipt,
            "notes": notes,
            "callback_url": settings.razorpay_callback_url,
            "callback_method": "get",
            "partial_payment": False,
        }
        if expires_at is not None:
            body["expire_by"] = int(expires_at.timestamp())
        try:
            # Blocking network I/O (the SDK wraps `requests`) moved off the
            # event loop — this runs on every online checkout, so without
            # `to_thread` it would stall every other concurrent request this
            # worker is serving for as long as Razorpay takes to answer
            # (AAD-PAY-008).
            link = await asyncio.to_thread(self._client.payment_link.create, body)
        except BadRequestError:
            # AAD-PAY-013: Razorpay rejected the request itself — a
            # malformed amount, an unsupported currency, bad auth from a
            # rotated key. This is our bug, not a transient gateway problem,
            # and retrying the identical request will fail identically.
            # Never wrap this as UpstreamError, which tells the caller (and,
            # through PaymentFailed-style handling upstream, potentially the
            # customer) "try again" — that's the wrong instinct for a defect
            # that only a code or config change can fix. Let it propagate as
            # the internal error it is, loud in the logs, not disguised as
            # gateway flakiness.
            log.exception(
                "razorpay rejected the payment link request — check the "
                "request shape or credentials, this will not self-resolve",
                extra={"receipt": receipt},
            )
            raise
        except Exception as exc:
            # Genuinely transient/upstream: GatewayError, ServerError, a
            # network timeout, or anything else the SDK didn't classify.
            # Worth telling the caller to retry.
            log.exception("razorpay payment link creation failed", extra={"receipt": receipt})
            raise UpstreamError("Could not start the payment. Try again.") from exc

        return ProviderOrder(
            provider=self.name,
            provider_order_id=link["id"],
            amount_paise=amount_paise,
            currency=currency,
            checkout_payload={
                "provider": self.name,
                "short_url": link["short_url"],
            },
        )

    def verify_payment_link_callback(
        self,
        *,
        payment_link_id: str,
        payment_link_reference_id: str,
        payment_link_status: str,
        payment_id: str,
        signature: str,
    ) -> bool:
        """Payment Links use a different signature scheme from Standard
        Checkout — a different set of fields, in a fixed order, joined with
        `|`. This is Razorpay's own documented formula for this flow; do not
        substitute the Standard Checkout formula here, the two are not
        interchangeable.
        """
        message = (
            f"{payment_link_id}|{payment_link_reference_id}|"
            f"{payment_link_status}|{payment_id}"
        ).encode()
        expected = hmac.new(self._secret, message, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    def parse_webhook(
        self, *, body: bytes, signature: str, event_id: str | None = None
    ) -> WebhookEvent:
        expected = hmac.new(self._webhook_secret, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            log.warning("razorpay webhook signature mismatch")
            raise PaymentFailed("Webhook signature verification failed.")

        payload: dict[str, Any] = json.loads(body)
        event_type = payload.get("event", "")
        inner = payload.get("payload", {})
        payment_entity = inner.get("payment", {}).get("entity") or {}
        payment_link_entity = inner.get("payment_link", {}).get("entity") or {}
        refund_entity = inner.get("refund", {}).get("entity") or {}
        order_entity = inner.get("order", {}).get("entity") or {}

        # A `payment_link.*` event carries the Payment Link's own entity
        # alongside a *different* Razorpay Order object's payment entity
        # (auto-created internally for the link) — that payment entity's
        # `order_id` does not refer to anything this app stored, and using
        # it here was AAD-PAY-007 defect 3. The payment_link entity's own
        # `id` is what create_order stored as provider_order_id, so it takes
        # priority whenever it is present.
        if payment_link_entity:
            provider_order_id = payment_link_entity.get("id")
        elif order_entity:
            provider_order_id = order_entity.get("id")
        elif payment_entity:
            provider_order_id = payment_entity.get("order_id")
        else:
            provider_order_id = None

        # `reference_id` is set to our own order id at link creation (see
        # create_order) — when the gateway hands it back, matching on it
        # directly is more robust than matching on a gateway-assigned id
        # whose provenance has to be inferred, per the audit's own guidance.
        order_id = payment_link_entity.get("reference_id") if payment_link_entity else None

        provider_payment_id = (
            (payment_entity.get("id") if payment_entity else None)
            or (refund_entity.get("payment_id") if refund_entity else None)
        )

        amount_paise = None
        if payment_entity:
            amount_paise = payment_entity.get("amount")
        elif payment_link_entity:
            amount_paise = payment_link_entity.get("amount_paid")
        elif refund_entity:
            amount_paise = refund_entity.get("amount")
        elif order_entity:
            amount_paise = order_entity.get("amount")

        return WebhookEvent(
            # AAD-PAY-009: Razorpay sends the event id in the
            # `x-razorpay-event-id` request header, not in the body — there
            # is no top-level "id" in the webhook JSON, so `payload.get("id")`
            # was always None and this always fell back to a body hash (a
            # *content* key, not an *event* key: two distinct events that
            # happen to serialise identically would collapse into one, and a
            # stored row couldn't be correlated with the delivery attempt in
            # Razorpay's own dashboard). `event_id` is that header, read and
            # passed in by the route. The body hash is now only a fallback
            # for a caller that genuinely didn't have the header available.
            event_id=event_id or hashlib.sha256(body).hexdigest(),
            event_type=event_type,
            provider_order_id=provider_order_id,
            provider_payment_id=provider_payment_id,
            amount_paise=amount_paise,
            raw=payload,
            order_id=order_id,
        )

    async def poll_status(self, *, provider_order_id: str) -> WebhookEvent | None:
        """Fetch a Payment Link directly instead of waiting for the webhook
        or the redirect — see `PaymentProvider.poll_status`. `provider_order_id`
        here is the link id stored on the order's payment row.
        """
        try:
            # Same defect class as create_order below (AAD-PAY-008): this is
            # the reconciliation backstop called before an abandoned
            # checkout's hold is treated as truly dead, so it still runs on
            # a real request path and must not block the loop either.
            link = await asyncio.to_thread(self._client.payment_link.fetch, provider_order_id)
        except Exception as exc:
            log.exception(
                "razorpay payment link status poll failed",
                extra={"provider_order_id": provider_order_id},
            )
            raise UpstreamError("Could not reach the payment gateway.") from exc

        if link.get("status") != "paid":
            return None

        payments_on_link = link.get("payments") or []
        provider_payment_id = None
        if payments_on_link:
            last = payments_on_link[-1]
            provider_payment_id = last.get("payment_id") or last.get("id")

        return WebhookEvent(
            event_id=f"poll_{link.get('id', provider_order_id)}",
            event_type="payment_link.paid",
            provider_order_id=link.get("id", provider_order_id),
            provider_payment_id=provider_payment_id,
            amount_paise=link.get("amount_paid"),
            raw={"source": "poll_status", "status": link.get("status")},
            order_id=link.get("reference_id"),
        )

    async def refund(
        self, *, provider_payment_id: str, amount_paise: int, notes: dict[str, str]
    ) -> str:
        try:
            # Same defect class as create_order above (AAD-PAY-008).
            refund = await asyncio.to_thread(
                self._client.payment.refund,
                provider_payment_id,
                {"amount": amount_paise, "notes": notes},
            )
        except BadRequestError:
            # AAD-PAY-013: same distinction as create_order above — a
            # malformed amount (e.g. more than remains refundable) or bad
            # auth is our bug, not the gateway being unreachable. Both
            # callers of this method (the refund_pending / amount_mismatch
            # sweeps) currently catch broadly and retry next sweep either
            # way, so this doesn't yet change outcome — only the log — but
            # it means the two failure modes are distinguishable here
            # rather than indistinguishable, which is what a fix beyond
            # this one (routing a permanently-bad refund to a dead letter
            # instead of retrying it forever) would need to build on.
            log.exception(
                "razorpay rejected the refund request — check the amount or "
                "credentials, this will not self-resolve on retry",
                extra={"payment_id": provider_payment_id},
            )
            raise
        except Exception as exc:
            log.exception("razorpay refund failed", extra={"payment_id": provider_payment_id})
            raise UpstreamError("Refund could not be processed.") from exc
        return refund["id"]
