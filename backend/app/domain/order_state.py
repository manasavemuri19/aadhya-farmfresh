"""The order lifecycle, expressed once.

Every status change in the system goes through `assert_transition`. Keeping the
graph in one place is what stops the classic quick-commerce bug where an order
is marked delivered after it was already cancelled and refunded.
"""

from __future__ import annotations

from app.core.errors import InvalidStateTransition
from app.domain.enums import OrderStatus

_ALLOWED: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PENDING_PAYMENT: frozenset({OrderStatus.CONFIRMED, OrderStatus.CANCELLED}),
    OrderStatus.CONFIRMED: frozenset({OrderStatus.PACKED, OrderStatus.CANCELLED}),
    OrderStatus.PACKED: frozenset({OrderStatus.OUT_FOR_DELIVERY, OrderStatus.CANCELLED}),
    OrderStatus.OUT_FOR_DELIVERY: frozenset({OrderStatus.DELIVERED, OrderStatus.CANCELLED}),
    OrderStatus.DELIVERED: frozenset({OrderStatus.REFUNDED}),
    OrderStatus.CANCELLED: frozenset({OrderStatus.REFUNDED}),
    OrderStatus.REFUNDED: frozenset(),
}

# Statuses at which reserved stock must be released back to the shelf.
RELEASES_STOCK: frozenset[OrderStatus] = frozenset(
    {OrderStatus.CANCELLED, OrderStatus.REFUNDED}
)

# Statuses the customer is still allowed to cancel from without calling the farm.
CUSTOMER_CANCELLABLE: frozenset[OrderStatus] = frozenset(
    {OrderStatus.PENDING_PAYMENT, OrderStatus.CONFIRMED, OrderStatus.PACKED}
)

TERMINAL: frozenset[OrderStatus] = frozenset(
    {OrderStatus.DELIVERED, OrderStatus.REFUNDED}
)


def can_transition(current: OrderStatus, target: OrderStatus) -> bool:
    return target in _ALLOWED.get(current, frozenset())


def assert_transition(current: OrderStatus, target: OrderStatus) -> None:
    if not can_transition(current, target):
        raise InvalidStateTransition(
            f"An order that is {current.value.replace('_', ' ')} cannot become "
            f"{target.value.replace('_', ' ')}.",
            details={"from": current.value, "to": target.value},
        )


def next_statuses(current: OrderStatus) -> list[OrderStatus]:
    return sorted(_ALLOWED.get(current, frozenset()), key=lambda s: s.value)
