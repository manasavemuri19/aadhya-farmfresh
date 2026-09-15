"""The order lifecycle, expressed once.

Every status change in the system goes through `assert_transition`. Keeping the
graph in one place is what stops the classic quick-commerce bug where an order
is marked delivered after it was already cancelled and refunded.
"""

from __future__ import annotations

from app.core.errors import InvalidStateTransition
from app.domain.enums import OrderStatus

_ALLOWED: dict[OrderStatus, frozenset[OrderStatus]] = {
    # PENDING_PAYMENT has no route to REFUNDED (AAD-PAY-002) — a payment
    # that was never captured cannot be refunded.
    OrderStatus.PENDING_PAYMENT: frozenset({OrderStatus.CONFIRMED, OrderStatus.CANCELLED}),
    # CONFIRMED/PACKED/OUT_FOR_DELIVERY → REFUNDED (AAD-PAY-002): a refund
    # issued straight from the gateway dashboard can land on any paid,
    # non-terminal order, not just one that was cancelled or delivered
    # first — the graph needs a reachable route from every one of them.
    OrderStatus.CONFIRMED: frozenset(
        {OrderStatus.PACKED, OrderStatus.CANCELLED, OrderStatus.REFUNDED}
    ),
    OrderStatus.PACKED: frozenset(
        {OrderStatus.OUT_FOR_DELIVERY, OrderStatus.CANCELLED, OrderStatus.REFUNDED}
    ),
    OrderStatus.OUT_FOR_DELIVERY: frozenset(
        {OrderStatus.DELIVERED, OrderStatus.CANCELLED, OrderStatus.REFUNDED}
    ),
    OrderStatus.DELIVERED: frozenset({OrderStatus.REFUNDED}),
    OrderStatus.CANCELLED: frozenset({OrderStatus.REFUNDED}),
    OrderStatus.REFUNDED: frozenset(),
}

# Target statuses that always route through `_cancel` — the shared path that
# transitions the order, conditionally restocks (see `releases_stock` below),
# queues any refund, and notifies the customer. This governs *routing*
# only — it is not itself the stock-crediting decision; that decision needs
# to know where the order came from too (AAD-PAY-004).
CANCEL_OR_REFUND_TARGETS: frozenset[OrderStatus] = frozenset(
    {OrderStatus.CANCELLED, OrderStatus.REFUNDED}
)

# Statuses at which the goods themselves have not yet left the building.
# Restocking only makes sense from here — see `releases_stock` below.
_NOT_YET_DISPATCHED: frozenset[OrderStatus] = frozenset(
    {OrderStatus.PENDING_PAYMENT, OrderStatus.CONFIRMED, OrderStatus.PACKED}
)


def releases_stock(from_status: OrderStatus, to_status: OrderStatus) -> bool:
    """Whether cancelling/refunding this specific transition should credit
    reserved stock back to the shelf (AAD-PAY-004).

    This used to be a plain set of *target* statuses (`RELEASES_STOCK`,
    checked as `new_status in RELEASES_STOCK`) — which made it a function of
    only where the order was going, not where it came from. Two legal
    transitions broke that assumption: `DELIVERED → REFUNDED` (a goodwill
    refund after the milk was already handed over) and
    `OUT_FOR_DELIVERY → CANCELLED` (the goods are already on the bike).
    Both credited the full quantity back, inflating stock on every such
    order — sell milk you don't have, get more cancellations, get more
    phantom stock.

    Restocking is only correct when the goods never left in the first
    place: `PENDING_PAYMENT`, `CONFIRMED` or `PACKED`. From
    `OUT_FOR_DELIVERY` or `DELIVERED`, the order is being written off, not
    returned to the shelf — see the write-off branch in `_cancel`.
    """
    if to_status not in CANCEL_OR_REFUND_TARGETS:
        return False
    return from_status in _NOT_YET_DISPATCHED

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
