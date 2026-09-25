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
    async def cancel_order(self, *, provider_order_id: str) -> None:
        """Best-effort void of a gateway order that must never be paid.

        AAD-PAY-016: `create_order` above is deliberately called before the
        local write that reserves stock and persists the order (see
        `OrderService`'s own module docstring) — a slow gateway must never
        hold inventory locks. That ordering means a failure *after* the
        gateway call but before the order is actually written (stock
        disappeared, a DB error, a lost compare-and-swap on a retry) leaves
        a real, live, payable gateway order with nothing behind it in this
        app at all. This is the caller's cleanup for exactly that case —
        called right after such a failure, before it's shown to the
        customer.

        Implementations must not raise: this runs from inside an
        already-failing path, and a secondary failure here must never
        replace or mask the original error the caller is propagating. Best
        effort — if the gateway can't be reached, or refuses because the
        order was already paid or already dead, the caller has nothing
        further to do; log it and return. A provider with no real backing
        gateway state to cancel (the mock provider) has nothing to do but
        record that it was asked, for tests.
        """
        ...

    @abstractmethod
    def parse_webhook(
        self, *, body: bytes, signature: str, event_id: str | None = None
    ) -> WebhookEvent:
        """Verify the signature and decode the event. Raises on a bad signature.

        `event_id` is the gateway's own delivery id (AAD-PAY-009) — the route
        reads it from whatever header the provider actually sends it in
        (`X-Razorpay-Event-Id` for Razorpay) and passes it through, since a
        provider's replay guard needs its *own* event id, not one guessed
        from the body, to correlate a stored `webhook_events` row with the
        delivery attempt in the gateway's own dashboard.
        """

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

    # AAD-QUAL-022: `sign_for_testing` and `verify_payment_link_callback`
    # below are not abstract — only one concrete provider each actually
    # implements them (the mock provider's own test-signing helper; the
    # Razorpay Payment Links callback formula). They live here, not as
    # `@abstractmethod`s every provider must define, so routes can call
    # `payments.sign_for_testing(...)` / `payments.verify_payment_link_callback(...)`
    # straight through this interface, with no `assert isinstance(payments,
    # <ConcreteClass>)` needed to satisfy the type checker first. The
    # `settings.payment_provider` check each caller already does before
    # reaching these is what actually guarantees the right provider is
    # configured; if that guard were ever wrong, the `NotImplementedError`
    # below is the clear, on-purpose failure a route sees instead of an
    # `AttributeError` from a missing method, or a passing-but-wrong
    # `isinstance` narrowing that `python -O` silently strips.
    def sign_for_testing(self, provider_order_id: str, provider_payment_id: str) -> str:
        raise NotImplementedError(f"{self.name} does not support sign_for_testing")

    def verify_payment_link_callback(
        self,
        *,
        payment_link_id: str,
        payment_link_reference_id: str,
        payment_link_status: str,
        payment_id: str,
        signature: str,
    ) -> bool:
        raise NotImplementedError(
            f"{self.name} does not support verify_payment_link_callback"
        )
