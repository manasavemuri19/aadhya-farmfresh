from __future__ import annotations

from functools import lru_cache

from app.core.config import settings
from app.payments.base import PaymentProvider, ProviderOrder, WebhookEvent
from app.payments.mock import MockPaymentProvider

__all__ = ["PaymentProvider", "ProviderOrder", "WebhookEvent", "get_payment_provider"]


@lru_cache(maxsize=1)
def get_payment_provider() -> PaymentProvider:
    if settings.payment_provider == "razorpay":
        from app.payments.razorpay import RazorpayProvider

        return RazorpayProvider()
    return MockPaymentProvider()
