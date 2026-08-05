from __future__ import annotations

import pytest

from app.core.errors import InvalidStateTransition
from app.domain.enums import OrderStatus
from app.domain.order_state import (
    CUSTOMER_CANCELLABLE,
    RELEASES_STOCK,
    TERMINAL,
    assert_transition,
    can_transition,
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


def test_terminal_statuses_never_release_stock_twice():
    # Delivered must not release stock; it was sold.
    assert OrderStatus.DELIVERED not in RELEASES_STOCK
    assert OrderStatus.CANCELLED in RELEASES_STOCK
    assert OrderStatus.REFUNDED in RELEASES_STOCK


def test_customer_cannot_cancel_once_out_for_delivery():
    assert OrderStatus.OUT_FOR_DELIVERY not in CUSTOMER_CANCELLABLE
    assert OrderStatus.DELIVERED not in CUSTOMER_CANCELLABLE
    assert OrderStatus.CONFIRMED in CUSTOMER_CANCELLABLE


def test_delivered_is_terminal_for_fulfilment():
    assert OrderStatus.DELIVERED in TERMINAL
