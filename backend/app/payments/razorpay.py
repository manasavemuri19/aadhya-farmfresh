"""Razorpay implementation of the payment contract.

Two signature schemes are involved and they are not the same thing:

  * the *checkout* signature is HMAC over "order_id|payment_id" using the API
    secret, returned by the client SDK when the sheet closes;
  * the *webhook* signature is HMAC over the raw request body using a separate
    webhook secret configured in the Razorpay dashboard.

Both are verified with `hmac.compare_digest`. The webhook is authoritative for
money; the checkout signature only lets the app show a success screen sooner.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

import razorpay

from app.core.config import settings
from app.core.errors import PaymentFailed, UpstreamError
from app.payments.base import PaymentProvider, ProviderOrder, WebhookEvent

log = logging.getLogger(__name__)


class RazorpayProvider(PaymentProvider):
    name = "razorpay"

    def __init__(self) -> None:
        if not (settings.razorpay_key_id and settings.razorpay_key_secret):
            raise RuntimeError("Razorpay credentials are not configured")
        self._client = razorpay.Client(
            auth=(settings.razorpay_key_id, settings.razorpay_key_secret)
        )
        self._secret = settings.razorpay_key_secret.encode()
        self._webhook_secret = settings.razorpay_webhook_secret.encode()

    async def create_order(
        self, *, amount_paise: int, currency: str, receipt: str, notes: dict[str, str]
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
        """
        try:
            link = self._client.payment_link.create(
                {
                    "amount": amount_paise,
                    "currency": currency,
                    "description": "Aadya Dairy order",
                    "reference_id": receipt,
                    "notes": notes,
                    "callback_url": settings.razorpay_callback_url,
                    "callback_method": "get",
                }
            )
        except Exception as exc:
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

    def verify_checkout_signature(
        self, *, provider_order_id: str, provider_payment_id: str, signature: str
    ) -> bool:
        message = f"{provider_order_id}|{provider_payment_id}".encode()
        expected = hmac.new(self._secret, message, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    def parse_webhook(self, *, body: bytes, signature: str) -> WebhookEvent:
        expected = hmac.new(self._webhook_secret, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            log.warning("razorpay webhook signature mismatch")
            raise PaymentFailed("Webhook signature verification failed.")

        payload: dict[str, Any] = json.loads(body)
        event_type = payload.get("event", "")
        entity = (
            payload.get("payload", {}).get("payment", {}).get("entity")
            or payload.get("payload", {}).get("refund", {}).get("entity")
            or payload.get("payload", {}).get("order", {}).get("entity")
            or {}
        )
        return WebhookEvent(
            # Razorpay sends this header-derived id; fall back to a body hash so
            # the replay guard still has a stable key.
            event_id=payload.get("id") or hashlib.sha256(body).hexdigest(),
            event_type=event_type,
            provider_order_id=entity.get("order_id") or entity.get("id"),
            provider_payment_id=entity.get("payment_id") or entity.get("id"),
            amount_paise=entity.get("amount"),
            raw=payload,
        )

    async def refund(
        self, *, provider_payment_id: str, amount_paise: int, notes: dict[str, str]
    ) -> str:
        try:
            refund = self._client.payment.refund(
                provider_payment_id, {"amount": amount_paise, "notes": notes}
            )
        except Exception as exc:
            log.exception("razorpay refund failed", extra={"payment_id": provider_payment_id})
            raise UpstreamError("Refund could not be processed.") from exc
        return refund["id"]
