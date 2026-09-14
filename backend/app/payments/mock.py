"""In-process payment provider for local development and tests.

Deterministic and offline: signatures are HMACs over a fixed dev secret, so the
full checkout flow — create order, verify, webhook, refund — can be exercised
end to end without touching a gateway. `Settings.assert_production_safe` refuses
to boot production with this provider selected.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime
from typing import Any

from app.core.errors import PaymentFailed
from app.core.ids import new_id
from app.payments.base import PaymentProvider, ProviderOrder, WebhookEvent

_DEV_SECRET = b"mock-provider-development-secret"


def _sign(message: str) -> str:
    return hmac.new(_DEV_SECRET, message.encode(), hashlib.sha256).hexdigest()


class MockPaymentProvider(PaymentProvider):
    name = "mock"

    async def create_order(
        self,
        *,
        amount_paise: int,
        currency: str,
        receipt: str,
        notes: dict[str, str],
        expires_at: datetime | None = None,
    ) -> ProviderOrder:
        # The mock provider has no gateway-side expiry to set — orders are
        # confirmed synchronously via /payments/mock/complete in tests and
        # local dev, never by a timer. Accepted for interface parity only.
        provider_order_id = new_id("mockord", 14)
        return ProviderOrder(
            provider=self.name,
            provider_order_id=provider_order_id,
            amount_paise=amount_paise,
            currency=currency,
            checkout_payload={
                "provider": self.name,
                "order_id": provider_order_id,
                "amount": amount_paise,
                "currency": currency,
                "receipt": receipt,
                # The client "pays" by echoing this back to the verify endpoint.
                "dev_hint": "POST /v1/payments/verify with any payment id and "
                            "the signature from /v1/payments/mock/sign",
            },
        )

    def verify_checkout_signature(
        self, *, provider_order_id: str, provider_payment_id: str, signature: str
    ) -> bool:
        expected = _sign(f"{provider_order_id}|{provider_payment_id}")
        return hmac.compare_digest(expected, signature)

    def sign_for_testing(self, provider_order_id: str, provider_payment_id: str) -> str:
        return _sign(f"{provider_order_id}|{provider_payment_id}")

    def parse_webhook(self, *, body: bytes, signature: str) -> WebhookEvent:
        expected = hmac.new(_DEV_SECRET, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise PaymentFailed("Webhook signature verification failed.")
        payload: dict[str, Any] = json.loads(body)
        return WebhookEvent(
            event_id=payload.get("event_id", new_id("evt", 12)),
            event_type=payload.get("event", "payment.captured"),
            provider_order_id=payload.get("order_id"),
            provider_payment_id=payload.get("payment_id"),
            amount_paise=payload.get("amount"),
            raw=payload,
        )

    async def poll_status(self, *, provider_order_id: str) -> WebhookEvent | None:
        """The mock provider has no external gateway to poll — in tests and
        local dev, confirmation always arrives synchronously through
        `/payments/mock/complete` instead, so there is nothing a poll could
        ever discover that hasn't already been applied. Always `None`."""
        return None

    async def refund(
        self, *, provider_payment_id: str, amount_paise: int, notes: dict[str, str]
    ) -> str:
        return new_id("mockrfnd", 12) + f"-{int(time.time())}"
