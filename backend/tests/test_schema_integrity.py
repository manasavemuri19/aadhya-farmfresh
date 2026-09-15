"""AAD-DATA-009 / AAD-DATA-010 / AAD-DATA-011 / AAD-DATA-008.

Four independent schema-integrity fixes, tested together since they're all
"does the database actually enforce what the code assumes" checks:

- AAD-DATA-009: the MRP check was strict `>`, blocking list-price selling.
- AAD-DATA-010: six enum-like text columns had no CHECK constraint at all.
- AAD-DATA-011: webhook payloads never expired.
- AAD-DATA-008: 0003's downgrade silently assumed no Google accounts exist.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.errors import ValidationError
from app.db.models import Order as OrderRow
from app.db.models import OrderEvent as OrderEventRow
from app.db.models import Payment as PaymentRow
from app.db.models import User as UserRow
from app.db.models import Variant as VariantRow
from app.db.models import WebhookEvent as WebhookEventRow
from app.schemas.catalog import Variant

# ---------- AAD-DATA-009: relaxed MRP check ----------


async def test_variant_schema_allows_price_equal_to_mrp():
    """Was a hard rejection before this fix — list-price selling is legal."""
    v = Variant(
        sku="TEST-SKU", label="1 unit", pack_value=1, pack_unit="piece",
        price_paise=5000, mrp_paise=5000,
    )
    assert v.mrp_paise == v.price_paise


async def test_variant_schema_still_rejects_mrp_below_price():
    with pytest.raises(PydanticValidationError, match="mrp_paise must be at least"):
        Variant(
            sku="TEST-SKU", label="1 unit", pack_value=1, pack_unit="piece",
            price_paise=5000, mrp_paise=4999,
        )


async def test_database_allows_price_equal_to_mrp(engine, session, milk):
    """Direct UPDATE, same shape as the audit's own reproduction."""
    await session.execute(
        update(VariantRow).where(VariantRow.sku == "MILK-COW-1L").values(price_paise=4000)
    )
    await session.flush()
    row = (
        await session.execute(select(VariantRow.price_paise, VariantRow.mrp_paise).where(
            VariantRow.sku == "MILK-COW-1L"
        ))
    ).one()
    assert row.price_paise == row.mrp_paise == 4000


async def test_database_still_rejects_mrp_below_price(engine, session, milk):
    await session.commit()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as bad_session:
        with pytest.raises(IntegrityError):
            await bad_session.execute(
                update(VariantRow).where(VariantRow.sku == "MILK-COW-1L").values(
                    price_paise=9000  # mrp_paise stays 4000 from the fixture
                )
            )
            await bad_session.commit()


async def test_set_price_can_raise_price_to_match_existing_mrp(session, products, milk):
    """MILK-COW-1L starts at price=3500, mrp=4000. Raising price to 4000
    with no mrp_paise given must succeed — this was flatly impossible before
    the fix, since price could only ever move down."""
    ok = await products.set_price("MILK-COW-1L", 4000)
    assert ok is True
    row = (
        await session.execute(select(VariantRow.price_paise).where(VariantRow.sku == "MILK-COW-1L"))
    ).scalar_one()
    assert row == 4000


async def test_set_price_raising_above_current_mrp_without_moving_mrp_is_a_clean_422(
    session, products, milk
):
    """The constraint violation must surface as a clean ValidationError, not
    a raw IntegrityError (which FastAPI's generic handler turns into a 500).
    confirm_large_change=True isolates this from AAD-API-006's separate
    large-price-change check (Batch 4c) — this test is specifically about
    the MRP relationship, not the change-size guard."""
    with pytest.raises(ValidationError, match="mrp_paise must be at least"):
        await products.set_price("MILK-COW-1L", 9000, confirm_large_change=True)
    # And the surrounding session must still be usable afterwards — the
    # SAVEPOINT must have contained the failure.
    row = (
        await session.execute(select(VariantRow.price_paise).where(VariantRow.sku == "MILK-COW-1L"))
    ).scalar_one()
    assert row == 3500  # unchanged


async def test_set_price_can_raise_price_and_mrp_together(session, products, milk):
    # confirm_large_change=True: 3500 -> 9000 is past AAD-API-006's ±50%
    # threshold (Batch 4c) — a deliberate large change here, not the point
    # of this test.
    ok = await products.set_price("MILK-COW-1L", 9000, 9500, confirm_large_change=True)
    assert ok is True
    row = (
        await session.execute(
            select(VariantRow.price_paise, VariantRow.mrp_paise)
            .where(VariantRow.sku == "MILK-COW-1L")
        )
    ).one()
    assert (row.price_paise, row.mrp_paise) == (9000, 9500)


async def test_set_price_request_schema_rejects_mrp_below_price():
    from app.schemas.order import SetPriceRequest

    with pytest.raises(PydanticValidationError, match="mrp_paise must be at least"):
        SetPriceRequest(sku="MILK-COW-1L", price_paise=5000, mrp_paise=4000)


# ---------- AAD-DATA-010: CHECK constraints on enum-like columns ----------


async def test_database_rejects_invalid_user_role(engine, session, user):
    await session.commit()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as bad_session:
        with pytest.raises(IntegrityError):
            await bad_session.execute(
                update(UserRow).where(UserRow.id == user["id"]).values(role="superadmin")
            )
            await bad_session.commit()


async def test_database_accepts_every_valid_user_role(engine, session, user):
    for role in ("customer", "staff", "admin", "delivery_agent"):
        await session.execute(update(UserRow).where(UserRow.id == user["id"]).values(role=role))
        await session.flush()


async def test_database_rejects_invalid_variant_stock_policy(engine, session, milk):
    await session.commit()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as bad_session:
        with pytest.raises(IntegrityError):
            await bad_session.execute(
                update(VariantRow).where(VariantRow.sku == "MILK-COW-1L").values(
                    stock_policy="whenever"
                )
            )
            await bad_session.commit()


async def _make_order(session, user) -> str:
    from app.core.ids import new_id

    order = OrderRow(
        id=new_id("ord", 12),
        order_number="AD-260101-0001",
        user_id=user["id"],
        status="confirmed",
        subtotal_paise=1000,
        delivery_fee_paise=0,
        discount_paise=0,
        total_paise=1000,
        address={"line1": "x", "city": "Hyderabad", "pincode": "500001"},
    )
    session.add(order)
    await session.flush()
    return order.id


async def test_database_rejects_invalid_order_status(engine, session, user):
    order_id = await _make_order(session, user)
    await session.commit()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as bad_session:
        with pytest.raises(IntegrityError):
            await bad_session.execute(
                update(OrderRow).where(OrderRow.id == order_id).values(status="banana")
            )
            await bad_session.commit()


async def test_database_accepts_every_valid_order_status(engine, session, user):
    order_id = await _make_order(session, user)
    for status in (
        "pending_payment", "confirmed", "packed", "out_for_delivery",
        "delivered", "cancelled", "refunded",
    ):
        await session.execute(update(OrderRow).where(OrderRow.id == order_id).values(status=status))
        await session.flush()


async def test_database_rejects_invalid_order_event_status(engine, session, user):
    order_id = await _make_order(session, user)
    await session.commit()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as bad_session:
        with pytest.raises(IntegrityError):
            bad_session.add(OrderEventRow(order_id=order_id, status="banana", by="test"))
            await bad_session.flush()


async def test_database_rejects_invalid_payment_status(engine, session, user):
    from app.core.ids import new_id

    order_id = await _make_order(session, user)
    session.add(
        PaymentRow(
            id=new_id("pay", 12), order_id=order_id, method="online",
            status="created", amount_paise=1000,
        )
    )
    await session.commit()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as bad_session:
        with pytest.raises(IntegrityError):
            await bad_session.execute(
                update(PaymentRow).where(PaymentRow.order_id == order_id).values(status="pending")
            )
            await bad_session.commit()


async def test_database_rejects_invalid_payment_method(engine, session, user):
    from app.core.ids import new_id

    order_id = await _make_order(session, user)
    session.add(
        PaymentRow(
            id=new_id("pay", 12), order_id=order_id, method="online",
            status="created", amount_paise=1000,
        )
    )
    await session.commit()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as bad_session:
        with pytest.raises(IntegrityError):
            await bad_session.execute(
                update(PaymentRow).where(PaymentRow.order_id == order_id).values(method="cheque")
            )
            await bad_session.commit()


# ---------- AAD-DATA-011: webhook payload retention ----------


async def test_redact_clears_payload_past_retention(session, orders):
    old = WebhookEventRow(
        provider="razorpay", event_id="evt_old", payload={"secret": "payment info"},
        received_at=datetime.now(UTC) - timedelta(days=61),
    )
    session.add(old)
    await session.flush()

    count = await orders.redact_expired_webhook_payloads()
    assert count == 1

    row = (
        await session.execute(
            select(WebhookEventRow.payload).where(WebhookEventRow.event_id == "evt_old")
        )
    ).scalar_one()
    assert row == {}


async def test_redact_leaves_recent_payload_untouched(session, orders):
    recent = WebhookEventRow(
        provider="razorpay", event_id="evt_recent", payload={"secret": "payment info"},
        received_at=datetime.now(UTC) - timedelta(days=1),
    )
    session.add(recent)
    await session.flush()

    count = await orders.redact_expired_webhook_payloads()
    assert count == 0

    row = (
        await session.execute(
            select(WebhookEventRow.payload).where(WebhookEventRow.event_id == "evt_recent")
        )
    ).scalar_one()
    assert row == {"secret": "payment info"}


async def test_redact_does_not_rewrite_already_redacted_rows(session, orders):
    """The != '{}' guard: a second sweep over an already-redacted row should
    not count it again."""
    already = WebhookEventRow(
        provider="razorpay", event_id="evt_already", payload={},
        received_at=datetime.now(UTC) - timedelta(days=200),
    )
    session.add(already)
    await session.flush()

    count = await orders.redact_expired_webhook_payloads()
    assert count == 0


# ---------- AAD-DATA-008: honest downgrade ----------


def test_0003_downgrade_raises_instead_of_failing_opaquely():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "alembic" / "versions" / "0003_google_auth.py"
    spec = importlib.util.spec_from_file_location("_0003_google_auth", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with pytest.raises(NotImplementedError, match="restore from a pre-migration snapshot"):
        module.downgrade()
