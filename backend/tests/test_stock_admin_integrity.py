"""Batch 4c — stock & admin integrity.

AAD-DATA-016 (set_stock CAS, and the real oversell it defeats), AAD-DATA-015
(the stock ledger recording the wrong number, fixed as a side effect of the
CAS), AAD-SEC-025 (admin-only price changes and refund transitions),
AAD-DATA-017 (catalog_audit trail on price/availability/bulk-upsert
changes), AAD-API-006 (bounded inputs and a large-price-change confirmation)
and AAD-OPS-017 (scripts/seed.py's production delete guard).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.deps import Principal
from app.api.v1.routes.admin import adjust_stock, update_order_status
from app.core.errors import Forbidden
from app.db.models import CatalogAudit as CatalogAuditRow
from app.db.models import StockLedger as StockLedgerRow
from app.db.models import Variant as VariantRow
from app.domain.enums import OrderStatus
from app.schemas.order import AdjustStockRequest, SetPriceRequest, UpdateOrderStatusRequest
from scripts.seed import authorize_retirement

# ---------- AAD-DATA-016: set_stock is a compare-and-swap ----------


async def test_set_stock_succeeds_when_expected_matches(session, products, milk):
    ok, value = await products.set_stock("MILK-COW-1L", 40, expected_qty=5)
    assert (ok, value) == (True, 40)
    row = (
        await session.execute(select(VariantRow.stock_qty).where(VariantRow.sku == "MILK-COW-1L"))
    ).scalar_one()
    assert row == 40


async def test_set_stock_reports_current_value_on_stale_expectation(session, products, milk):
    """MILK-COW-1L starts at stock_qty=5. Claiming the screen showed 40 (it
    didn't) must be refused, with the real current value handed back."""
    ok, value = await products.set_stock("MILK-COW-1L", 40, expected_qty=40)
    assert (ok, value) == (False, 5)
    row = (
        await session.execute(select(VariantRow.stock_qty).where(VariantRow.sku == "MILK-COW-1L"))
    ).scalar_one()
    assert row == 5  # unchanged


async def test_set_stock_reports_none_for_missing_sku(session, products):
    ok, value = await products.set_stock("NOPE", 40, expected_qty=0)
    assert (ok, value) == (False, None)


async def test_reservation_between_load_and_save_makes_a_stale_set_stock_fail(
    engine, session, milk
):
    """The audit's own reproduction, made real: staff's screen shows 40 at
    7:00; between then and 7:02 a customer reservation (a real, separate
    connection, matching test_concurrency_real.py's pattern) drops it to
    39; staff confirm the stale "40" at 7:02. Before this fix that
    overwrite succeeded and silently erased the reservation. Now it's
    refused — the CAS sees the current value is 39, not the 40 staff's
    screen expected."""
    from app.repositories.products import ProductRepository

    await session.execute(
        VariantRow.__table__.update().where(VariantRow.sku == "MILK-COW-1L").values(stock_qty=40)
    )
    await session.commit()

    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as reservation_session:
        reserved = await ProductRepository(reservation_session).reserve_stock("MILK-COW-1L", 1)
        await reservation_session.commit()
    assert reserved is True

    async with factory() as staff_session:
        ok, current = await ProductRepository(staff_session).set_stock(
            "MILK-COW-1L", 40, expected_qty=40
        )
        assert (ok, current) == (False, 39)


# ---------- AAD-DATA-015: the ledger records the real delta ----------


async def test_adjust_stock_route_records_the_real_delta_not_the_absolute_value(
    session, products, milk, idempotency
):
    """Before this fix, a `set_qty` adjustment wrote the *absolute* new
    value to the ledger as if it were a delta — staff correcting a count
    from 5 to 40 would log a delta of +40, not the real +35. AAD-DATA-016's
    CAS makes `set_qty - expected_qty` finally trustworthy (the write only
    lands when expected_qty was accurate), and the route now uses that real
    difference instead of the absolute value. MILK-COW-1L starts at 5."""
    await adjust_stock(
        AdjustStockRequest(sku="MILK-COW-1L", set_qty=40, expected_qty=5, reason="morning_count"),
        Principal("usr_staff1", "staff"),
        products,
        idempotency,
    )
    await session.flush()  # record_stock_movement only session.add()s
    # AAD-DATA-004: the `milk` fixture's own upsert_product call now writes
    # its own opening_balance entry (the fix for a starting stock_qty that
    # used to enter the ledger with no record at all) — filtered out here
    # since this test is only about the adjust_stock call's own entry.
    row = (
        await session.execute(
            select(StockLedgerRow).where(
                StockLedgerRow.sku == "MILK-COW-1L",
                StockLedgerRow.reason != "opening_balance",
            )
        )
    ).scalars().one()
    assert row.delta == 35  # 40 - 5, not the absolute 40


# ---------- AAD-DATA-017: catalog_audit ----------


async def _audit_rows(session, sku: str) -> list[CatalogAuditRow]:
    # record_catalog_change only `session.add()`s — this session has
    # autoflush=False (conftest.py), so a pending audit row needs an
    # explicit flush before a plain select() will see it (same gotcha
    # documented in test_order_flow.py's write_off_entries helper).
    await session.flush()
    return list(
        (await session.execute(select(CatalogAuditRow).where(CatalogAuditRow.sku == sku)))
        .scalars()
        .all()
    )


async def test_set_price_records_an_audit_row_for_a_changed_price(session, products, milk):
    await products.set_price("MILK-COW-1L", 3600, actor="usr_staff1", source="admin_api")
    rows = await _audit_rows(session, "MILK-COW-1L")
    assert len(rows) == 1
    assert rows[0].field == "price_paise"
    assert rows[0].old_value == "3500"
    assert rows[0].new_value == "3600"
    assert rows[0].actor == "usr_staff1"
    assert rows[0].source == "admin_api"


async def test_set_price_records_no_audit_row_when_price_is_unchanged(session, products, milk):
    ok = await products.set_price("MILK-COW-1L", 3500)  # already 3500
    assert ok is True
    assert await _audit_rows(session, "MILK-COW-1L") == []


async def test_set_variant_active_records_an_audit_row(session, products, milk):
    await products.set_variant_active("MILK-COW-1L", False, actor="usr_staff1")
    rows = await _audit_rows(session, "MILK-COW-1L")
    assert len(rows) == 1
    assert (rows[0].field, rows[0].old_value, rows[0].new_value) == ("is_active", "True", "False")


async def test_set_variant_active_records_no_row_when_unchanged(session, products, milk):
    await products.set_variant_active("MILK-COW-1L", True)  # already active
    assert await _audit_rows(session, "MILK-COW-1L") == []


async def test_upsert_product_records_audit_rows_for_changed_fields(session, products, milk):
    from app.schemas.catalog import Product as ProductSchema
    from app.schemas.catalog import Variant as VariantSchema

    updated = ProductSchema(
        id="prd_placeholder", slug="full-cream-cow-milk", name="Full Cream Cow Milk",
        description="Farm fresh", category="milk", prep_minutes=20,
        variants=[
            VariantSchema(
                sku="MILK-COW-1L", label="1 litre", pack_value=1, pack_unit="l",
                price_paise=3700, mrp_paise=4000, stock_qty=5, max_per_order=10,
            ),
            VariantSchema(
                sku="MILK-COW-500ML", label="500 ml", pack_value=500, pack_unit="ml",
                price_paise=2000, stock_qty=0,
            ),
        ],
    )
    await products.upsert_product(updated, actor="system", source="seed")
    rows = await _audit_rows(session, "MILK-COW-1L")
    assert len(rows) == 1
    assert (rows[0].field, rows[0].old_value, rows[0].new_value) == ("price_paise", "3500", "3700")
    assert rows[0].source == "seed"
    # The untouched 500ml variant must not get a spurious row.
    assert await _audit_rows(session, "MILK-COW-500ML") == []


async def test_upsert_product_records_no_row_for_a_brand_new_variant(session, products, milk):
    from app.schemas.catalog import Product as ProductSchema
    from app.schemas.catalog import Variant as VariantSchema

    updated = ProductSchema(
        id="prd_placeholder", slug="full-cream-cow-milk", name="Full Cream Cow Milk",
        description="Farm fresh", category="milk", prep_minutes=20,
        variants=[
            VariantSchema(
                sku="MILK-COW-1L", label="1 litre", pack_value=1, pack_unit="l",
                price_paise=3500, mrp_paise=4000, stock_qty=5, max_per_order=10,
            ),
            VariantSchema(
                sku="MILK-COW-500ML", label="500 ml", pack_value=500, pack_unit="ml",
                price_paise=2000, stock_qty=0,
            ),
            VariantSchema(
                sku="MILK-COW-2L", label="2 litre", pack_value=2, pack_unit="l",
                price_paise=6500, stock_qty=0,
            ),
        ],
    )
    await products.upsert_product(updated)
    assert await _audit_rows(session, "MILK-COW-2L") == []


async def test_upsert_product_records_a_row_when_a_variant_is_retired(session, products, milk):
    from app.schemas.catalog import Product as ProductSchema
    from app.schemas.catalog import Variant as VariantSchema

    # Drop the 500ml variant from the incoming catalog — upsert_product
    # deactivates rather than deletes orphaned variants.
    updated = ProductSchema(
        id="prd_placeholder", slug="full-cream-cow-milk", name="Full Cream Cow Milk",
        description="Farm fresh", category="milk", prep_minutes=20,
        variants=[
            VariantSchema(
                sku="MILK-COW-1L", label="1 litre", pack_value=1, pack_unit="l",
                price_paise=3500, mrp_paise=4000, stock_qty=5, max_per_order=10,
            ),
        ],
    )
    await products.upsert_product(updated)
    rows = await _audit_rows(session, "MILK-COW-500ML")
    assert len(rows) == 1
    assert (rows[0].field, rows[0].old_value, rows[0].new_value) == ("is_active", "True", "False")


# ---------- AAD-API-006: bounded inputs + large-price-change confirmation ----------


def test_adjust_stock_request_rejects_a_quantity_over_the_ceiling():
    with pytest.raises(PydanticValidationError):
        AdjustStockRequest(sku="MILK-COW-1L", set_qty=1_000_000, expected_qty=5)


def test_adjust_stock_request_rejects_an_oversized_delta():
    with pytest.raises(PydanticValidationError):
        AdjustStockRequest(sku="MILK-COW-1L", delta_qty=-1_000_000)


def test_set_price_request_rejects_a_price_over_max_paise():
    with pytest.raises(PydanticValidationError):
        SetPriceRequest(sku="MILK-COW-1L", price_paise=200_000_000)


async def test_set_price_large_change_without_confirmation_is_refused(session, products, milk):
    from app.core.errors import ValidationError

    with pytest.raises(ValidationError, match="more than 50%"):
        await products.set_price("MILK-COW-1L", 9000)  # 3500 -> 9000, +157%


async def test_set_price_large_change_with_confirmation_succeeds(session, products, milk):
    # mrp_paise bumped alongside price: milk's fixture MRP is 4000, and this
    # test is about the large-change guard, not AAD-DATA-009's separate MRP
    # relationship (see test_schema_integrity.py for that one).
    ok = await products.set_price("MILK-COW-1L", 9000, 9500, confirm_large_change=True)
    assert ok is True


async def test_set_price_small_change_needs_no_confirmation(session, products, milk):
    ok = await products.set_price("MILK-COW-1L", 3600)  # +2.9%
    assert ok is True


async def test_set_price_from_zero_always_needs_confirmation(session, products, milk):
    from app.core.errors import ValidationError

    await products.set_price("MILK-COW-1L", 0, confirm_large_change=True)
    with pytest.raises(ValidationError, match="more than 50%"):
        await products.set_price("MILK-COW-1L", 1)  # any move off zero


# ---------- AAD-SEC-025: admin-only price changes and refund transitions ----------


def test_set_price_route_requires_admin_not_just_staff():
    """`SetPriceRequest`'s `admin` parameter must depend on `require_admin`,
    not `require_staff` — a regression here would silently let any staff
    account change prices again. `Annotated[Principal, Depends(...)]` type
    hints don't affect a direct Python call (that's why every other test in
    this file calls the route function directly), so this specific wiring
    can only be checked by inspecting the annotation itself. `require_admin`
    vs `require_staff`'s own behaviour is already proven independently in
    test_privileged_deps.py; this only guards the route's wiring to it."""
    import typing

    from app.api.deps import require_admin
    from app.api.v1.routes.admin import set_price

    hints = typing.get_type_hints(set_price, include_extras=True)
    depends = hints["admin"].__metadata__[0]
    assert depends.dependency is require_admin


async def test_refund_transition_is_refused_for_a_staff_account():
    """Called directly — a staff Principal, and svc=None, which would blow
    up on any attribute access. The guard must fire before svc is touched
    at all."""
    body = UpdateOrderStatusRequest(status=OrderStatus.REFUNDED, note="")
    with pytest.raises(Forbidden, match="owner account"):
        await update_order_status(
            order_id="doesnt-matter", body=body,
            staff=Principal("usr_staff1", "staff"), svc=None,
        )


async def test_refund_transition_is_allowed_for_an_admin_account():
    calls: list[dict] = []

    class _StubService:
        async def update_status(self, **kwargs):
            calls.append(kwargs)
            return "sentinel"

    body = UpdateOrderStatusRequest(status=OrderStatus.REFUNDED, note="damaged in transit")
    result = await update_order_status(
        order_id="ord_1", body=body, staff=Principal("usr_admin1", "admin"), svc=_StubService(),
    )
    assert result == "sentinel"
    assert calls == [
        {"order_id": "ord_1", "new_status": OrderStatus.REFUNDED,
         "note": "damaged in transit", "actor": "usr_admin1"}
    ]


async def test_non_refund_transitions_still_work_for_staff():
    """The guard is REFUNDED-specific — ordinary fulfilment transitions,
    which is what the staff role is for, must be unaffected."""
    calls: list[dict] = []

    class _StubService:
        async def update_status(self, **kwargs):
            calls.append(kwargs)
            return "sentinel"

    body = UpdateOrderStatusRequest(status=OrderStatus.PACKED, note="")
    result = await update_order_status(
        order_id="ord_1", body=body, staff=Principal("usr_staff1", "staff"), svc=_StubService(),
    )
    assert result == "sentinel"
    assert len(calls) == 1


# ---------- AAD-OPS-017: scripts/seed.py's production delete guard ----------


def test_authorize_retirement_is_a_noop_with_nothing_to_retire():
    authorize_retirement([], force=False, is_production=True, interactive=False)  # no raise


def test_authorize_retirement_blocks_production_without_force():
    with pytest.raises(SystemExit, match="Refusing to delete"):
        authorize_retirement(
            ["stale-product"], force=False, is_production=True, interactive=False
        )


def test_authorize_retirement_blocks_production_even_when_interactive():
    """Production is refused unconditionally without --force — this must
    not depend on whether a human happens to be at a terminal."""
    with pytest.raises(SystemExit, match="Refusing to delete"):
        authorize_retirement(
            ["stale-product"], force=False, is_production=True, interactive=True,
            confirm_input=lambda: "delete",
        )


def test_authorize_retirement_allows_production_with_force():
    authorize_retirement(["stale-product"], force=True, is_production=True, interactive=False)


def test_authorize_retirement_prompts_and_aborts_on_a_wrong_answer():
    with pytest.raises(SystemExit, match="Aborted"):
        authorize_retirement(
            ["stale-product"], force=False, is_production=False, interactive=True,
            confirm_input=lambda: "no thanks",
        )


def test_authorize_retirement_prompts_and_proceeds_on_confirmation():
    authorize_retirement(
        ["stale-product"], force=False, is_production=False, interactive=True,
        confirm_input=lambda: "delete",
    )  # no raise


def test_authorize_retirement_is_silent_when_non_interactive_and_not_production():
    """CI or a cron reseed of a dev/staging environment — unattended, and
    not production — must keep working exactly as before this fix."""
    authorize_retirement(
        ["stale-product"], force=False, is_production=False, interactive=False
    )  # no raise, no prompt
