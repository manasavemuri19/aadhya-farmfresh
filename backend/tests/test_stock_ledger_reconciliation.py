"""AAD-DATA-004 — the stock ledger's one job is `SUM(delta) GROUP BY sku`
reconstructing `variants.stock_qty` for a discrepancy dispute. A
MADE_TO_ORDER variant's stock_qty is never decremented by `reserve_stock`
(there's no shelf count to hold) or credited by `release_stock` — correct —
but the ledger used to record `delta=-qty` / `delta=+qty` for it anyway on
every order and every cancel, unconditionally. Summed against a stock_qty
that never moved, those phantom entries never balance.

`set_stock`/`set_price` writing no audit trail at all, and `set_stock`
being a blind CAS-less overwrite — the other two gaps this finding
originally named — turned out to already be fixed (AAD-DATA-015/016/017,
an earlier session): `set_stock` requires `expected_qty` and always writes
a `record_stock_movement` row (see `POST /admin/stock` in routes/admin.py),
and `set_price` always writes a `CatalogAudit` row. Confirmed by reading
both call sites, not assumed — see the audit doc for where each check
happened. Nothing further needed there; this file only covers the
made-to-order phantom-entry gap, the one part still actually broken.
"""

from __future__ import annotations

from sqlalchemy import func, select

from app.db.models import StockLedger
from app.domain.enums import OrderStatus, PaymentMethod
from tests.test_order_flow import order_request, stock_of


async def _ledger_sum(products, sku: str) -> int:
    await products.session.flush()
    result = await products.session.execute(
        select(func.coalesce(func.sum(StockLedger.delta), 0)).where(StockLedger.sku == sku)
    )
    return result.scalar_one()


async def _ledger_rows(products, sku: str, order_id: str) -> list[StockLedger]:
    await products.session.flush()
    result = await products.session.execute(
        select(StockLedger).where(StockLedger.sku == sku, StockLedger.order_id == order_id)
    )
    return list(result.scalars().all())


async def test_ordering_a_made_to_order_item_records_no_phantom_stock_movement(
    order_service, user, products, khoya
):
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("KHOYA-250G", 2)], payment_method=PaymentMethod.COD),
        idempotency_key=None,
    )

    rows = await _ledger_rows(products, "KHOYA-250G", order.id)
    assert len(rows) == 1
    assert rows[0].delta == 0
    assert rows[0].reason == "order_reserved_made_to_order"

    # The one real invariant this ledger exists to protect: summed against
    # actual stock_qty (which a made-to-order variant never moves from 0).
    assert await _ledger_sum(products, "KHOYA-250G") == await stock_of(products, "KHOYA-250G")


async def test_cancelling_a_made_to_order_order_records_no_phantom_credit(
    order_service, user, products, khoya
):
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("KHOYA-250G", 2)], payment_method=PaymentMethod.COD),
        idempotency_key=None,
    )

    await order_service.cancel(order_id=order.id, user_id=user["id"], reason="changed my mind")

    rows = await _ledger_rows(products, "KHOYA-250G", order.id)
    assert len(rows) == 2
    reserve, release = rows[0], rows[1]
    assert reserve.reason == "order_reserved_made_to_order" and reserve.delta == 0
    assert release.reason == "order_cancelled_made_to_order" and release.delta == 0

    assert await _ledger_sum(products, "KHOYA-250G") == await stock_of(products, "KHOYA-250G")


async def test_a_tracked_item_still_records_real_stock_movements(
    order_service, user, products, milk
):
    """Regression guard: the made-to-order fix must not flatten every
    reason/delta to zero — an ordinary TRACKED SKU still needs its real
    stock movements recorded, exactly as before this fix."""
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 3)], payment_method=PaymentMethod.COD),
        idempotency_key=None,
    )
    rows = await _ledger_rows(products, "MILK-COW-1L", order.id)
    assert len(rows) == 1
    assert rows[0].delta == -3
    assert rows[0].reason == "order_reserved"

    await order_service.cancel(order_id=order.id, user_id=user["id"], reason="changed my mind")
    rows = await _ledger_rows(products, "MILK-COW-1L", order.id)
    assert len(rows) == 2
    assert rows[1].delta == 3
    assert rows[1].reason == "order_cancelled"

    # opening_balance(+5, from the fixture's upsert_product) + reserved(-3)
    # + cancelled(+3) == 5 == the variant's actual, unchanged stock_qty.
    assert await _ledger_sum(products, "MILK-COW-1L") == 5
    assert await _ledger_sum(products, "MILK-COW-1L") == await stock_of(products, "MILK-COW-1L")


async def test_a_new_tracked_variant_gets_an_opening_balance_entry(session, products, milk):
    """AAD-DATA-004: a brand-new TRACKED variant's starting stock_qty used
    to enter the system with no ledger row at all — the `milk` fixture's
    own upsert_product call is the case in point, seeding MILK-COW-1L at
    stock_qty=5. Every reconciliation of that SKU would have been short by
    5 forever, for no reason a human could see in the ledger itself."""
    rows = await _ledger_rows(products, "MILK-COW-1L", None)  # no order_id: not order-driven
    assert len(rows) == 1
    assert rows[0].delta == 5
    assert rows[0].reason == "opening_balance"
    assert await _ledger_sum(products, "MILK-COW-1L") == await stock_of(products, "MILK-COW-1L")


async def test_a_new_zero_stock_variant_gets_no_opening_balance_entry(session, products, milk):
    """MILK-COW-500ML is seeded at stock_qty=0 — nothing to reconcile, so
    nothing worth a ledger row for."""
    rows = await _ledger_rows(products, "MILK-COW-500ML", None)
    assert rows == []


async def test_a_new_made_to_order_variant_gets_no_opening_balance_entry(session, products, khoya):
    rows = await _ledger_rows(products, "KHOYA-250G", None)
    assert rows == []


async def test_find_stock_discrepancies_is_clean_after_normal_activity(
    order_service, user, products, milk, khoya
):
    """The reconciliation query itself: after a mix of ordinary order,
    cancel and made-to-order activity, every TRACKED variant's ledger sum
    must equal its live stock_qty — nothing left for a nightly check to
    flag."""
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request(
            [("MILK-COW-1L", 2), ("KHOYA-250G", 1)], payment_method=PaymentMethod.COD
        ),
        idempotency_key=None,
    )
    await order_service.cancel(order_id=order.id, user_id=user["id"], reason="changed my mind")

    # record_stock_movement only session.add()s (this session has
    # autoflush=False, per conftest.py) — a real caller would normally be
    # reading after an earlier transaction already committed, but this test
    # shares one, so it needs the same explicit flush every other ledger
    # read in this file already does.
    await products.session.flush()
    assert await products.find_stock_discrepancies() == []


async def test_find_stock_discrepancies_flags_a_real_mismatch(session, products, milk):
    """Prove the query actually detects a problem, not just that it stays
    quiet — directly corrupt stock_qty (bypassing every code path that
    would normally ledger the change) to simulate the kind of drift this
    check exists to catch."""
    from app.db.models import Variant as VariantRow

    await session.execute(
        VariantRow.__table__.update().where(VariantRow.sku == "MILK-COW-1L").values(stock_qty=999)
    )
    await session.flush()

    discrepancies = await products.find_stock_discrepancies()
    assert len(discrepancies) == 1
    assert discrepancies[0]["sku"] == "MILK-COW-1L"
    assert discrepancies[0]["stock_qty"] == 999
    assert discrepancies[0]["ledger_sum"] == 5  # the opening balance, untouched
    assert discrepancies[0]["discrepancy"] == 994


async def test_write_off_after_dispatch_is_unaffected_by_this_fix(
    order_service, user, orders, products, milk
):
    """The write-off path (goods already out for delivery) already recorded
    delta=0 for every line regardless of stock policy — this fix doesn't
    touch it, and it must keep working exactly as before (AAD-PAY-004)."""
    from app.payments.base import WebhookEvent

    order = await order_service.create_order(
        user_id=user["id"], request=order_request([("MILK-COW-1L", 3)]), idempotency_key=None
    )
    doc = await orders.get(order.id)
    await order_service.apply_webhook(
        WebhookEvent(
            event_id=f"evt_{order.id}", event_type="payment.captured",
            provider_order_id=doc["payment"]["provider_order_id"],
            provider_payment_id=f"pay_{order.id}", amount_paise=order.total_paise, raw={},
        )
    )
    for status in (OrderStatus.PACKED, OrderStatus.OUT_FOR_DELIVERY):
        await order_service.update_status(
            order_id=order.id, new_status=status, note=status.value, actor="agent_1"
        )

    await order_service.update_status(
        order_id=order.id, new_status=OrderStatus.CANCELLED,
        note="refused at the door", actor="agent_1",
    )

    rows = await _ledger_rows(products, "MILK-COW-1L", order.id)
    write_off = next(r for r in rows if r.reason.endswith("_write_off"))
    assert write_off.delta == 0
    assert await stock_of(products, "MILK-COW-1L") == 2  # never credited back
