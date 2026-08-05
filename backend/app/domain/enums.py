from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    CUSTOMER = "customer"
    STAFF = "staff"       # farm counter: manages stock and fulfils orders
    ADMIN = "admin"       # owner: everything, including refunds


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


class PaymentMethod(StrEnum):
    ONLINE = "online"     # UPI / card / netbanking via the gateway
    COD = "cod"           # cash or UPI-on-delivery


class StockPolicy(StrEnum):
    """How a SKU's availability is decided."""

    TRACKED = "tracked"       # decremented per order; blocks when exhausted
    MADE_TO_ORDER = "made_to_order"  # always sellable within the daily cutoff
