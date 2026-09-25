"""AAD-BIZ-002 — cash on delivery used to have no abuse controls at all: no
limit on concurrent unpaid COD orders per user, no cap on order value, and
no history check. An account created in seconds via Google sign-in could
place unlimited high-value COD orders to arbitrary addresses; fresh dairy
would be prepared, dispatched, refused, and thrown away.

This covers the two controls that are on by default (a per-order value cap
and a per-user concurrency cap) and the one that exists but defaults off
pending a product decision (requiring a prior delivered order). See
`OrderService._check_cod_eligibility`'s own docstring for what this
deliberately does not cover (a refusal-rate threshold) and why.
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.core.errors import CodUnavailableError
from app.domain.enums import OrderStatus, PaymentMethod
from app.schemas.auth import Address
from app.schemas.order import CartLineInput, CreateOrderRequest

ADDRESS = Address(label="Home", line1="12-3-45 Banjara Hills", city="Hyderabad", pincode="500034")


def _cod_request(lines) -> CreateOrderRequest:
    return CreateOrderRequest(
        lines=[CartLineInput(sku=s, qty=q) for s, q in lines],
        address=ADDRESS,
        payment_method=PaymentMethod.COD,
    )


def _online_request(lines) -> CreateOrderRequest:
    return CreateOrderRequest(
        lines=[CartLineInput(sku=s, qty=q) for s, q in lines],
        address=ADDRESS,
        payment_method=PaymentMethod.ONLINE,
    )


async def _advance_to_delivered(order_service, order_id: str) -> None:
    for status in (OrderStatus.PACKED, OrderStatus.OUT_FOR_DELIVERY, OrderStatus.DELIVERED):
        await order_service.update_status(
            order_id=order_id, new_status=status, note="advancing", actor="staff_1"
        )


# ---------- value cap ----------


async def test_a_cod_cart_over_the_value_cap_is_refused(
    order_service, user, products, milk, monkeypatch
):
    monkeypatch.setattr(settings, "cod_max_order_value_paise", 7_000)  # 1 unit + delivery = 6400

    with pytest.raises(CodUnavailableError) as exc:
        await order_service.create_order(
            user_id=user["id"], request=_cod_request([("MILK-COW-1L", 2)]),  # 12800 paise
            idempotency_key=None,
        )
    assert exc.value.details["limit_paise"] == 7_000


async def test_the_same_cart_can_still_be_placed_paying_online(
    order_service, user, products, milk, monkeypatch
):
    """The point of a 422 rather than a 403: nothing about the customer is
    refused, just this one payment method for this one cart."""
    monkeypatch.setattr(settings, "cod_max_order_value_paise", 7_000)

    order = await order_service.create_order(
        user_id=user["id"], request=_online_request([("MILK-COW-1L", 2)]),
        idempotency_key=None,
    )
    assert order.status == OrderStatus.PENDING_PAYMENT.value


async def test_a_cod_cart_at_or_under_the_cap_is_unaffected(
    order_service, user, products, milk, monkeypatch
):
    monkeypatch.setattr(settings, "cod_max_order_value_paise", 7_000)

    order = await order_service.create_order(
        user_id=user["id"], request=_cod_request([("MILK-COW-1L", 1)]),  # 6400 paise
        idempotency_key=None,
    )
    assert order.status == OrderStatus.CONFIRMED.value


async def test_a_refused_cod_order_never_reserves_stock_or_burns_an_order_number(
    order_service, orders, products, milk, user, monkeypatch
):
    """The check runs before pricing turns into a write — a refused COD
    cart must leave stock and the daily order-number sequence exactly as it
    found them, the same property AAD-DATA-001's own tests pin for
    OutOfStock."""
    monkeypatch.setattr(settings, "cod_max_order_value_paise", 7_000)

    with pytest.raises(CodUnavailableError):
        await order_service.create_order(
            user_id=user["id"], request=_cod_request([("MILK-COW-1L", 2)]),
            idempotency_key=None,
        )

    order = await order_service.create_order(
        user_id=user["id"], request=_cod_request([("MILK-COW-1L", 1)]),
        idempotency_key=None,
    )
    assert order.order_number.endswith("-0001")


# ---------- concurrency cap ----------


async def test_active_cod_orders_up_to_the_cap_are_allowed(
    order_service, user, products, milk, monkeypatch
):
    monkeypatch.setattr(settings, "cod_max_active_orders_per_user", 2)

    for _ in range(2):
        order = await order_service.create_order(
            user_id=user["id"], request=_cod_request([("MILK-COW-1L", 1)]),
            idempotency_key=None,
        )
        assert order.status == OrderStatus.CONFIRMED.value


async def test_a_cod_order_past_the_concurrency_cap_is_refused(
    order_service, user, products, milk, monkeypatch
):
    monkeypatch.setattr(settings, "cod_max_active_orders_per_user", 2)

    for _ in range(2):
        await order_service.create_order(
            user_id=user["id"], request=_cod_request([("MILK-COW-1L", 1)]),
            idempotency_key=None,
        )

    with pytest.raises(CodUnavailableError) as exc:
        await order_service.create_order(
            user_id=user["id"], request=_cod_request([("MILK-COW-1L", 1)]),
            idempotency_key=None,
        )
    assert exc.value.details["limit"] == 2


async def test_a_delivered_order_frees_up_a_concurrency_slot(
    order_service, user, products, milk, monkeypatch
):
    """The cap counts *active* COD orders, not lifetime ones — once one is
    delivered it stops counting against the limit."""
    monkeypatch.setattr(settings, "cod_max_active_orders_per_user", 1)

    first = await order_service.create_order(
        user_id=user["id"], request=_cod_request([("MILK-COW-1L", 1)]),
        idempotency_key=None,
    )
    with pytest.raises(CodUnavailableError):
        await order_service.create_order(
            user_id=user["id"], request=_cod_request([("MILK-COW-1L", 1)]),
            idempotency_key=None,
        )

    await _advance_to_delivered(order_service, first.id)

    second = await order_service.create_order(
        user_id=user["id"], request=_cod_request([("MILK-COW-1L", 1)]),
        idempotency_key=None,
    )
    assert second.status == OrderStatus.CONFIRMED.value


async def test_a_cancelled_order_also_frees_up_a_concurrency_slot(
    order_service, user, products, milk, monkeypatch
):
    monkeypatch.setattr(settings, "cod_max_active_orders_per_user", 1)

    first = await order_service.create_order(
        user_id=user["id"], request=_cod_request([("MILK-COW-1L", 1)]),
        idempotency_key=None,
    )
    await order_service.cancel(order_id=first.id, user_id=user["id"], reason="changed my mind")

    second = await order_service.create_order(
        user_id=user["id"], request=_cod_request([("MILK-COW-1L", 1)]),
        idempotency_key=None,
    )
    assert second.status == OrderStatus.CONFIRMED.value


async def test_the_concurrency_cap_is_per_user(
    order_service, user, products, milk, monkeypatch, session
):
    """Someone else's in-flight COD orders must never count against this
    user's own cap."""
    from app.repositories.users import UserRepository

    other = await UserRepository(session).get_or_create_by_google(
        google_sub="test_google_sub_other", email="other@example.com", name="Other User",
    )
    await session.flush()

    monkeypatch.setattr(settings, "cod_max_active_orders_per_user", 1)

    await order_service.create_order(
        user_id=other["id"], request=_cod_request([("MILK-COW-1L", 1)]),
        idempotency_key=None,
    )

    order = await order_service.create_order(
        user_id=user["id"], request=_cod_request([("MILK-COW-1L", 1)]),
        idempotency_key=None,
    )
    assert order.status == OrderStatus.CONFIRMED.value


# ---------- new-account gate (off by default) ----------


async def test_the_prior_delivery_gate_is_off_by_default(
    order_service, user, products, milk
):
    """A brand-new account with zero order history must still be able to
    place a COD order out of the box — this control is real but held
    behind a settings flag pending a product decision (see the module
    docstring), not silently turned on."""
    assert settings.cod_requires_prior_delivery is False

    order = await order_service.create_order(
        user_id=user["id"], request=_cod_request([("MILK-COW-1L", 1)]),
        idempotency_key=None,
    )
    assert order.status == OrderStatus.CONFIRMED.value


async def test_enabling_the_gate_refuses_cod_for_a_brand_new_account(
    order_service, user, products, milk, monkeypatch
):
    monkeypatch.setattr(settings, "cod_requires_prior_delivery", True)

    with pytest.raises(CodUnavailableError):
        await order_service.create_order(
            user_id=user["id"], request=_cod_request([("MILK-COW-1L", 1)]),
            idempotency_key=None,
        )


async def test_enabling_the_gate_still_allows_paying_online_for_the_first_order(
    order_service, user, products, milk, monkeypatch
):
    monkeypatch.setattr(settings, "cod_requires_prior_delivery", True)

    order = await order_service.create_order(
        user_id=user["id"], request=_online_request([("MILK-COW-1L", 1)]),
        idempotency_key=None,
    )
    assert order.status == OrderStatus.PENDING_PAYMENT.value


async def test_the_gate_unlocks_once_an_order_of_theirs_has_been_delivered(
    order_service, user, products, milk, monkeypatch
):
    # First order pays online, since the gate isn't active for it yet. Its
    # own payment never actually clears here — update_status below moves it
    # straight to CONFIRMED by hand, the same way staff manually confirming
    # a COD edge case does; only the resulting DELIVERED status matters to
    # this gate, not how the order got paid.
    first = await order_service.create_order(
        user_id=user["id"], request=_online_request([("MILK-COW-1L", 1)]),
        idempotency_key=None,
    )

    monkeypatch.setattr(settings, "cod_requires_prior_delivery", True)

    # Not delivered yet — still refused.
    with pytest.raises(CodUnavailableError):
        await order_service.create_order(
            user_id=user["id"], request=_cod_request([("MILK-COW-1L", 1)]),
            idempotency_key=None,
        )

    await order_service.update_status(
        order_id=first.id, new_status=OrderStatus.CONFIRMED, note="paid", actor="staff_1"
    )
    await _advance_to_delivered(order_service, first.id)

    unlocked = await order_service.create_order(
        user_id=user["id"], request=_cod_request([("MILK-COW-1L", 1)]),
        idempotency_key=None,
    )
    assert unlocked.status == OrderStatus.CONFIRMED.value
