"""AAD-PERF-007 — `OrderRepository.transition()` used to end every call with
`return await self.get(order_id)`: a full 4-query eager-loaded reload
(main row plus the `lines`/`events`/`payment` selectin queries), whether or
not the caller actually wanted a fresh view back.

`_cancel` was the sharpest case: it took that reload, then — only when
`_maybe_refund` had just written a payment-status change the first reload
couldn't have seen — took a *second* one to pick that write up, and threw
the first one away unused. `transition()` now just reports whether its CAS
won; callers fetch a view for themselves, exactly once, at the point they
actually need one. These tests prove the call count, not just that the
order ends up in the right state (every existing cancel/refund test in
test_order_flow.py already covers that, and still passes unchanged).
"""

from __future__ import annotations

from app.domain.enums import OrderStatus, PaymentMethod
from tests.test_order_flow import _place_and_capture, order_request


async def test_cancelling_a_captured_online_order_reloads_it_only_once(
    order_service, user, orders, milk, monkeypatch
):
    """Cancelling (via staff/`update_status`) a CONFIRMED order with a
    captured online payment is exactly the path that used to double-reload:
    `_maybe_refund` flips the payment to REFUND_PENDING, and the returned
    view has to reflect that. Before this fix that meant two calls to
    `orders.get` inside the cancel itself (one thrown away); now it's one.
    """
    doc = await _place_and_capture(order_service, orders, user, [("MILK-COW-1L", 3)])
    assert doc["payment"]["status"] == "captured"

    calls: list[str] = []
    real_get = orders.get

    async def counting_get(order_id: str):
        calls.append(order_id)
        return await real_get(order_id)

    monkeypatch.setattr(orders, "get", counting_get)

    await order_service.update_status(
        order_id=doc["id"], new_status=OrderStatus.CANCELLED,
        note="staff cancel", actor="staff_1",
    )

    # One `get` to read the order's current status at the top of
    # `update_status`, and exactly one more — inside `_cancel`, after
    # `_maybe_refund` — to build the view that gets returned and notified
    # on. Not the three calls this would have been before this fix (the
    # same initial read, plus one buried inside the old `transition()`,
    # plus the old unconditional post-refund re-read).
    assert calls == [doc["id"], doc["id"]]


async def test_cancelling_a_cod_order_still_reloads_it_only_once(
    order_service, user, orders, products, monkeypatch, milk
):
    """The far more common case — nothing captured, so `_maybe_refund` is a
    no-op — must not regress into an extra reload either; it was already
    one call inside `_cancel` before this fix, and stays one."""
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 3)], payment_method=PaymentMethod.COD),
        idempotency_key=None,
    )

    calls: list[str] = []
    real_get = orders.get

    async def counting_get(order_id: str):
        calls.append(order_id)
        return await real_get(order_id)

    monkeypatch.setattr(orders, "get", counting_get)

    await order_service.cancel(order_id=order.id, user_id=user["id"], reason="changed my mind")

    assert calls == [order.id]
