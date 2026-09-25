"""Batch 23: AAD-OPS-014 — service-level test coverage for `OrderService`.

The finding named seven gaps. Checking each against the suite as it stands
today (after every batch up through 22) found six of them already closed —
not by this batch, but as a side effect of the individual findings that
created each behavior getting their own regression test at the point they
were fixed, exactly the remediation this finding itself suggested ("Add a
service-level test per finding as each is fixed"):

  - a capture arriving after cancellation (AAD-PAY-001)
    -> tests/test_late_capture_refund.py (4 tests)
  - the refund webhook branch (AAD-PAY-002)
    -> tests/test_order_flow.py (test_dashboard_refund_before_dispatch_*,
       test_dashboard_refund_after_dispatch_*, test_duplicate_dashboard_
       refund_webhook_is_a_no_op, test_refund_webhook_for_never_captured_
       payment_does_not_crash)
  - restocking on DELIVERED -> REFUNDED (AAD-PAY-004)
    -> tests/test_order_flow.py (test_refunding_a_delivered_order_does_
       not_restock, test_dashboard_refund_after_dispatch_writes_off_not_
       restocks)
  - release_expired_holds
    -> tests/test_hold_sweep_hardening.py, tests/test_payment_link_
       confirmation.py, tests/test_amount_mismatch.py, tests/test_late_
       capture_refund.py all call it directly across several scenarios
  - update_status
    -> exercised across 10 existing test files (test_delivery.py,
       test_order_service_hygiene.py, test_order_flow.py, and others)

The seventh — pagination — is also already covered (tests/test_pagination.py,
4 tests across both the customer list and the staff queue), so it isn't
repeated here either.

The one genuine gap: nothing anywhere in the suite ever called
`OrderService.update_address` itself. `tests/test_address_serviceability.py`
covers the *schema*-level rejection of an out-of-bounds address (AAD-SEC-020),
including one test whose own docstring says outright that the bad address
"never got far enough to reach OrderService.update_address" — proving the
service method's own compare-and-swap logic (the status-window check, the
Forbidden/NotFound split) had no coverage at all. That's what this file adds.

Update, later in the engagement: writing the happy-path tests below surfaced
a genuine docstring-vs-code ambiguity (the method's docstring said editing
stops once an order is packed; the code it actually guarded with,
CUSTOMER_CANCELLABLE, is the *cancel* window and includes PACKED) — flagged
rather than guessed at, in §8 of the audit. The product decision that came
back: self-serve address edits are off entirely, at every status, once an
order is placed. `update_address` now always raises Forbidden (see its own
docstring for how — `expected_statuses=[]`, kept wired up rather than
deleted). The first three tests below were rewritten from "succeeds" to
"is forbidden" to match; the two NotFound tests are unaffected, since which
statuses are ever allowed doesn't change how a wrong user id or a
nonexistent order id is handled.
"""

from __future__ import annotations

import pytest

from app.core.errors import Forbidden, NotFound
from app.domain.enums import OrderStatus, PaymentMethod
from app.schemas.auth import Address
from app.schemas.order import CartLineInput, CreateOrderRequest

_HOME = Address(
    label="Home", line1="12-3-45 Banjara Hills", city="Hyderabad", pincode="500034",
    latitude=17.4200, longitude=78.6000,
)
_NEW_ADDRESS = Address(
    label="Work", line1="Cyber Towers, HITEC City", city="Hyderabad", pincode="500081",
    latitude=17.4483, longitude=78.3915,
)


def _order_request(**kw) -> CreateOrderRequest:
    return CreateOrderRequest(
        lines=[CartLineInput(sku="MILK-COW-1L", qty=1)], address=_HOME, **kw
    )


async def test_update_address_is_forbidden_on_a_confirmed_order(order_service, user, milk):
    order = await order_service.create_order(
        user_id=user["id"],
        request=_order_request(payment_method=PaymentMethod.COD),
        idempotency_key="batch23-address-1",
    )
    assert order.status == OrderStatus.CONFIRMED.value

    with pytest.raises(Forbidden, match="can't be changed once an order is placed"):
        await order_service.update_address(
            order_id=order.id, user_id=user["id"], address=_NEW_ADDRESS
        )

    # The order itself is untouched.
    reread = await order_service.get_for_user(order.id, user["id"])
    assert reread.address.line1 == "12-3-45 Banjara Hills"


async def test_update_address_is_forbidden_on_a_packed_order(order_service, user, milk):
    order = await order_service.create_order(
        user_id=user["id"],
        request=_order_request(payment_method=PaymentMethod.COD),
        idempotency_key="batch23-address-2",
    )
    await order_service.update_status(
        order_id=order.id, new_status=OrderStatus.PACKED, note="packed", actor="staff:test"
    )

    with pytest.raises(Forbidden, match="can't be changed once an order is placed"):
        await order_service.update_address(
            order_id=order.id, user_id=user["id"], address=_NEW_ADDRESS
        )


async def test_update_address_is_forbidden_once_out_for_delivery(order_service, user, milk):
    order = await order_service.create_order(
        user_id=user["id"],
        request=_order_request(payment_method=PaymentMethod.COD),
        idempotency_key="batch23-address-3",
    )
    await order_service.update_status(
        order_id=order.id, new_status=OrderStatus.PACKED, note="packed", actor="staff:test"
    )
    await order_service.update_status(
        order_id=order.id, new_status=OrderStatus.OUT_FOR_DELIVERY,
        note="picked up", actor="staff:test",
    )

    with pytest.raises(Forbidden, match="can't be changed once an order is placed"):
        await order_service.update_address(
            order_id=order.id, user_id=user["id"], address=_NEW_ADDRESS
        )

    # The order itself is untouched.
    reread = await order_service.get_for_user(order.id, user["id"])
    assert reread.address.line1 == "12-3-45 Banjara Hills"


async def test_update_address_raises_not_found_for_someone_elses_order(
    order_service, session, user, milk
):
    """The CAS in `OrderRepository.update_address` filters on `user_id` as
    well as status, so a second user pointed at the first user's order id
    finds zero rows the same way a wrong/expired status would — and the
    service's own fallback re-read (`get_for_user`, also `user_id`-scoped)
    correctly reports NotFound rather than leaking whether the order exists
    at all under someone else's id.
    """
    from app.repositories.users import UserRepository

    other_user = await UserRepository(session).get_or_create_by_google(
        google_sub="test_other_user_sub_0001", email="other@example.com", name="Other User",
    )
    order = await order_service.create_order(
        user_id=user["id"],
        request=_order_request(payment_method=PaymentMethod.COD),
        idempotency_key="batch23-address-4",
    )

    with pytest.raises(NotFound):
        await order_service.update_address(
            order_id=order.id, user_id=other_user["id"], address=_NEW_ADDRESS
        )


async def test_update_address_raises_not_found_for_a_nonexistent_order(order_service, user):
    with pytest.raises(NotFound):
        await order_service.update_address(
            order_id="ord_doesnotexist000", user_id=user["id"], address=_NEW_ADDRESS
        )
