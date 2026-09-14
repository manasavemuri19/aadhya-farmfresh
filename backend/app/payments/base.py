"""Payment provider contract.

The rest of the app talks to this interface only. Swapping Razorpay for
Cashfree, or adding a second provider for a different state, is a new class —
not a change to order logic. The `mock` provider makes the whole checkout flow
testable and demoable without any gateway account at all.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
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
    # Set only when the event itself carries *our own* order id — for a
    # Razorpay Payment Link this is `reference_id`, which `create_order` sets
    # to the order id at link creation (see RazorpayProvider.create_order).
    # When present, `apply_webhook` looks the order up by this directly
    # instead of matching on a gateway-assigned id (AAD-PAY-007).
    order_id: str | None = None

    @property
    def is_capture(self) -> bool:
        # `payment_link.partially_paid` is included deliberately, not by
        # oversight: Payment Links are created with partial payment disabled
        # (see RazorpayProvider.create_order), so this event should not occur
        # in practice. If it ever does, routing it through the capture path
        # means the existing amount-mismatch check in
        # `OrderService.apply_webhook` treats it as the incident it is,
        # rather than silently dropping it.
        return self.event_type in {
            "payment.captured",
            "order.paid",
            "payment_link.paid",
            "payment_link.partially_paid",
        }

    @property
    def is_failure(self) -> bool:
        return self.event_type in {
            "payment.failed",
            "payment_link.expired",
            "payment_link.cancelled",
        }

    @property
    def is_refund(self) -> bool:
        return self.event_type in {"refund.processed", "refund.created"}


class PaymentProvider(ABC):
    name: str

    @abstractmethod
    async def create_order(
        self,
        *,
        amount_paise: int,
        currency: str,
        receipt: str,
        notes: dict[str, str],
        expires_at: datetime | None = None,
    ) -> ProviderOrder:
        """`expires_at`, when given, is the same instant the order's payment
        hold expires (`OrderService.PAYMENT_HOLD`). Implementations that
        support it should set it as the gateway-side expiry too, so the app
        and the gateway cannot disagree about when a payment is dead
        (AAD-PAY-006 / AAD-PAY-014)."""
        ...

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
    async def poll_status(self, *, provider_order_id: str) -> WebhookEvent | None:
        """Ask the gateway directly whether this order has been paid.

        A reconciliation backstop only (AAD-PAY-006) — called before an
        abandoned checkout's payment hold is treated as truly abandoned, so a
        webhook or redirect that simply hasn't arrived yet does not cost the
        customer a cancelled, paid-for order. Returns a synthetic
        `WebhookEvent` equivalent to what a capture webhook would have
        delivered when the gateway confirms payment, or `None` when it does
        not (still pending, expired, or unknown to the gateway).
        """
        ...

    @abstractmethod
    async def refund(
        self, *, provider_payment_id: str, amount_paise: int, notes: dict[str, str]
    ) -> str: ...
