from __future__ import annotations

import pytest

from app.core.errors import InvalidStateTransition
from app.domain.enums import OrderStatus
from app.domain.order_state import (
    CANCEL_OR_REFUND_TARGETS,
    CUSTOMER_CANCELLABLE,
    TERMINAL,
    assert_transition,
    can_transition,
    releases_stock,
)


@pytest.mark.parametrize(
    "current,target",
    [
        (OrderStatus.PENDING_PAYMENT, OrderStatus.CONFIRMED),
        (OrderStatus.CONFIRMED, OrderStatus.PACKED),
        (OrderStatus.PACKED, OrderStatus.OUT_FOR_DELIVERY),
        (OrderStatus.OUT_FOR_DELIVERY, OrderStatus.DELIVERED),
        (OrderStatus.DELIVERED, OrderStatus.REFUNDED),
    ],
)
def test_happy_path_transitions_are_allowed(current, target):
    assert can_transition(current, target)


@pytest.mark.parametrize(
    "current,target",
    [
        # The bug this whole module exists to prevent.
        (OrderStatus.CANCELLED, OrderStatus.DELIVERED),
        (OrderStatus.REFUNDED, OrderStatus.CONFIRMED),
        (OrderStatus.DELIVERED, OrderStatus.CANCELLED),
        (OrderStatus.PENDING_PAYMENT, OrderStatus.DELIVERED),
        (OrderStatus.CONFIRMED, OrderStatus.OUT_FOR_DELIVERY),
    ],
)
def test_illegal_transitions_are_rejected(current, target):
    assert not can_transition(current, target)
    with pytest.raises(InvalidStateTransition):
        assert_transition(current, target)


def test_no_status_can_transition_to_itself():
    for status in OrderStatus:
        assert not can_transition(status, status)


def test_refunded_is_a_dead_end():
    for status in OrderStatus:
        assert not can_transition(OrderStatus.REFUNDED, status)


def test_cancel_or_refund_targets_still_route_through_cancel():
    # Just the routing set now (AAD-PAY-004) — whether stock actually gets
    # credited back is `releases_stock`'s job, tested below against real
    # stock counts in test_order_flow.py, not this frozenset.
    assert OrderStatus.CANCELLED in CANCEL_OR_REFUND_TARGETS
    assert OrderStatus.REFUNDED in CANCEL_OR_REFUND_TARGETS
    assert OrderStatus.DELIVERED not in CANCEL_OR_REFUND_TARGETS


@pytest.mark.parametrize(
    "from_status,to_status",
    [
        (OrderStatus.PENDING_PAYMENT, OrderStatus.CANCELLED),
        (OrderStatus.CONFIRMED, OrderStatus.CANCELLED),
        (OrderStatus.PACKED, OrderStatus.CANCELLED),
        (OrderStatus.PENDING_PAYMENT, OrderStatus.REFUNDED),
        (OrderStatus.CONFIRMED, OrderStatus.REFUNDED),
        (OrderStatus.PACKED, OrderStatus.REFUNDED),
    ],
)
def test_releases_stock_before_dispatch(from_status, to_status):
    assert releases_stock(from_status, to_status)


@pytest.mark.parametrize(
    "from_status,to_status",
    [
        # AAD-PAY-004's two named bugs: the goods already left the
        # building, so these must NOT credit stock back.
        (OrderStatus.OUT_FOR_DELIVERY, OrderStatus.CANCELLED),
        (OrderStatus.DELIVERED, OrderStatus.REFUNDED),
        # Same principle, the other transition each of those statuses allows.
        (OrderStatus.OUT_FOR_DELIVERY, OrderStatus.DELIVERED),
        # A target that isn't cancel/refund at all is trivially "no".
        (OrderStatus.PENDING_PAYMENT, OrderStatus.CONFIRMED),
    ],
)
def test_does_not_release_stock_after_dispatch_or_for_non_cancel_targets(from_status, to_status):
    assert not releases_stock(from_status, to_status)


def test_customer_cannot_cancel_once_out_for_delivery():
    assert OrderStatus.OUT_FOR_DELIVERY not in CUSTOMER_CANCELLABLE
    assert OrderStatus.DELIVERED not in CUSTOMER_CANCELLABLE
    assert OrderStatus.CONFIRMED in CUSTOMER_CANCELLABLE


def test_delivered_is_terminal_for_fulfilment():
    assert OrderStatus.DELIVERED in TERMINAL
