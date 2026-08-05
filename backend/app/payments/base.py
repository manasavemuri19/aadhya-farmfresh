"""Payment provider contract.

The rest of the app talks to this interface only. Swapping Razorpay for
Cashfree, or adding a second provider for a different state, is a new class —
not a change to order logic. The `mock` provider makes the whole checkout flow
testable and demoable without any gateway account at all.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ProviderOrder:
    provider: str
    provider_order_id: str
    amount_paise: int
    currency: str
    # Everything the mobile SDK needs to open the checkout sheet.
    checkout_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WebhookEvent:
    event_id: str
    event_type: str
    provider_order_id: str | None
    provider_payment_id: str | None
    amount_paise: int | None
    raw: dict[str, Any]

    @property
    def is_capture(self) -> bool:
        return self.event_type in {"payment.captured", "order.paid"}

    @property
    def is_failure(self) -> bool:
        return self.event_type == "payment.failed"

    @property
    def is_refund(self) -> bool:
        return self.event_type in {"refund.processed", "refund.created"}


class PaymentProvider(ABC):
    name: str

    @abstractmethod
    async def create_order(
        self, *, amount_paise: int, currency: str, receipt: str, notes: dict[str, str]
    ) -> ProviderOrder: ...

    @abstractmethod
    def verify_checkout_signature(
        self, *, provider_order_id: str, provider_payment_id: str, signature: str
    ) -> bool:
        """Verify the payload the client hands back after the sheet closes.

        A pass here is a hint, not proof of payment — the webhook is the only
        thing that moves an order to confirmed.
        """

    @abstractmethod
    def parse_webhook(self, *, body: bytes, signature: str) -> WebhookEvent:
        """Verify the signature and decode the event. Raises on a bad signature."""

    @abstractmethod
    async def refund(
        self, *, provider_payment_id: str, amount_paise: int, notes: dict[str, str]
    ) -> str: ...
