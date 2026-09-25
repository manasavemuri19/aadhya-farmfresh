"""AAD-API-004: cursor pagination for a customer's order history and the
staff order queue.

Before this fix, `GET /orders` exposed only `limit` (capped at 50) with no
way to reach anything past it, and the staff queue (`GET /admin/orders`,
`OrderRepository.list_by_status`) had a hard cap and no cursor at all —
orders past it were not an error, they simply never appeared to the people
who had to pack them. Both routes now go through `OrderService` methods
that fetch one row past `limit`, use it to compute `has_more`, and hand back
`next_cursor` (the last returned order's `created_at`, ISO-formatted) so the
caller can ask for the next page explicitly instead of finding out about
the cut-off by silence.

`created_at` is a `server_default=func.now()` column, and everything in a
test runs inside one open, uncommitted transaction (see conftest.py) — so
without help every order placed in a single test could get the *same*
`created_at`, which would make cursor ordering ambiguous. These tests place
real orders through `OrderService.create_order` (so the whole checkout path
still runs for real) and then backdate each row's `created_at` directly, to
pin down an unambiguous, known order for the cursor math to walk through.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import update

from app.core.config import settings
from app.db.models import Order as OrderRow
from app.domain.enums import OrderStatus, PaymentMethod
from app.schemas.auth import Address
from app.schemas.order import CartLineInput, CreateOrderRequest

ADDRESS = Address(
    label="Home", line1="12-3-45 Banjara Hills", city="Hyderabad", pincode="500034"
)

BASE = datetime(2026, 1, 1, tzinfo=UTC)


async def _place_orders(order_service, session, user, n: int, monkeypatch) -> list[str]:
    """Places `n` one-unit COD orders (MILK-COW-1L has stock_qty=5, so
    n <= 5) and backdates them to BASE, BASE+1min, ... so `created_at` is
    strictly increasing in placement order regardless of transaction timing.
    Returns order ids in placement order (oldest first).

    AAD-BIZ-002's per-user COD cap (default 2 active orders) would
    otherwise stop this single customer partway through — raised to `n`
    since this file is testing pagination, not COD abuse limits."""
    monkeypatch.setattr(settings, "cod_max_active_orders_per_user", n)
    ids = []
    for i in range(n):
        request = CreateOrderRequest(
            lines=[CartLineInput(sku="MILK-COW-1L", qty=1)],
            address=ADDRESS,
            payment_method=PaymentMethod.COD,
        )
        order = await order_service.create_order(
            user_id=user["id"], request=request, idempotency_key=None
        )
        await session.execute(
            update(OrderRow)
            .where(OrderRow.id == order.id)
            .values(created_at=BASE + timedelta(minutes=i))
        )
        ids.append(order.id)
    await session.flush()
    return ids


async def test_list_for_user_reports_has_more_and_walks_back_with_the_cursor(
    order_service, session, user, milk, monkeypatch
):
    # Oldest first: order_ids[0] is BASE, order_ids[4] is BASE+4min.
    order_ids = await _place_orders(order_service, session, user, 5, monkeypatch)
    newest_first = list(reversed(order_ids))

    page1 = await order_service.list_for_user(user["id"], limit=2)
    assert [o.id for o in page1.items] == newest_first[0:2]
    assert page1.has_more is True
    assert page1.next_cursor is not None

    page2 = await order_service.list_for_user(
        user["id"], limit=2, before=datetime.fromisoformat(page1.next_cursor)
    )
    assert [o.id for o in page2.items] == newest_first[2:4]
    assert page2.has_more is True

    page3 = await order_service.list_for_user(
        user["id"], limit=2, before=datetime.fromisoformat(page2.next_cursor)
    )
    assert [o.id for o in page3.items] == newest_first[4:5]
    assert page3.has_more is False
    assert page3.next_cursor is None


async def test_list_for_user_has_more_is_false_when_a_page_exactly_fits(
    order_service, session, user, milk, monkeypatch
):
    await _place_orders(order_service, session, user, 2, monkeypatch)
    page = await order_service.list_for_user(user["id"], limit=2)
    assert len(page.items) == 2
    assert page.has_more is False
    assert page.next_cursor is None


async def test_staff_queue_reports_has_more_and_walks_forward_with_the_cursor(
    order_service, session, user, milk, monkeypatch
):
    # COD orders confirm immediately, so all 5 land in CONFIRMED — exactly
    # the queue's default `wanted` statuses.
    order_ids = await _place_orders(order_service, session, user, 5, monkeypatch)  # oldest first

    page1 = await order_service.list_queue_for_staff(
        [OrderStatus.CONFIRMED], limit=2
    )
    assert [o.id for o in page1.items] == order_ids[0:2]
    assert page1.has_more is True

    page2 = await order_service.list_queue_for_staff(
        [OrderStatus.CONFIRMED],
        limit=2,
        after=datetime.fromisoformat(page1.next_cursor),
    )
    assert [o.id for o in page2.items] == order_ids[2:4]
    assert page2.has_more is True

    page3 = await order_service.list_queue_for_staff(
        [OrderStatus.CONFIRMED],
        limit=2,
        after=datetime.fromisoformat(page2.next_cursor),
    )
    assert [o.id for o in page3.items] == order_ids[4:5]
    assert page3.has_more is False
    assert page3.next_cursor is None


async def test_staff_queue_no_longer_silently_drops_orders_past_the_cap(
    order_service, session, user, milk, monkeypatch
):
    """The bug this finding described directly: with a cap and no cursor,
    orders past the cap simply never appeared, with nothing in the response
    saying so. Confirm the same query today at least *reports* the cut via
    has_more, which is what a real client polls to know to ask for page 2."""
    await _place_orders(order_service, session, user, 5, monkeypatch)
    capped = await order_service.list_queue_for_staff([OrderStatus.CONFIRMED], limit=3)
    assert len(capped.items) == 3
    assert capped.has_more is True  # previously: no such signal existed at all
