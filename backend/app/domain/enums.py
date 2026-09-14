from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    CUSTOMER = "customer"
    STAFF = "staff"       # farm counter: manages stock and fulfils orders
    ADMIN = "admin"       # owner: everything, including refunds
    # Not self-serve — assigned the same way STAFF/ADMIN are (directly on the
    # user row), never chosen at signup. A delivery agent's app is otherwise
    # a completely separate 2-tab experience; see app/(tabs)/_layout.tsx.
    DELIVERY_AGENT = "delivery_agent"


class UserStatus(StrEnum):
    """AAD-SEC-002: checked on refresh and by the privileged-role
    dependencies — see `User.status` in db/models.py."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETED = "deleted"


class OrderStatus(StrEnum):
    PENDING_PAYMENT = "pending_payment"
    CONFIRMED = "confirmed"
    PACKED = "packed"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class PaymentStatus(StrEnum):
    CREATED = "created"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    FAILED = "failed"
    REFUNDED = "refunded"
    # AAD-PAY-003: a cancel/force-refund has committed and released stock,
    # but the actual gateway refund call happens later, out of that
    # request's transaction — this marks the gap between the two so it is
    # visible and retryable rather than silent.
    REFUND_PENDING = "refund_pending"
    # AAD-PAY-005: a capture webhook whose amount doesn't match the order
    # total. The money is already at the gateway, so this isn't a status
    # nothing-happened — it flags the payment for an automatic refund (via
    # the same out-of-transaction sweep REFUND_PENDING uses, since it's the
    # same kind of gateway call) and for a human to look at, because a
    # mismatch is either a gateway bug or an attack.
    AMOUNT_MISMATCH = "amount_mismatch"


class PaymentMethod(StrEnum):
    ONLINE = "online"     # UPI / card / netbanking via the gateway
    COD = "cod"           # cash or UPI-on-delivery


class StockPolicy(StrEnum):
    """How a SKU's availability is decided."""

    TRACKED = "tracked"       # decremented per order; blocks when exhausted
    MADE_TO_ORDER = "made_to_order"  # always sellable within the daily cutoff
