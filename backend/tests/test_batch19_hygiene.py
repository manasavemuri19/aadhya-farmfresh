"""Batch 19: SQL-side radius filtering for the delivery request queue,
one fewer full order re-read on a status update, escaped ILIKE wildcards
in catalog search, and the CREATE INDEX CONCURRENTLY convention.
"""

from __future__ import annotations

import pytest
from sqlalchemy import update as sa_update

from app.db.models import User as UserRow
from app.domain.enums import OrderStatus, PaymentMethod
from app.domain.geo import bounding_box_km, haversine_km
from app.repositories.delivery import DeliveryRepository
from app.repositories.users import UserRepository
from app.schemas.auth import Address
from app.schemas.order import CartLineInput, CreateOrderRequest
from app.services.delivery_service import DeliveryService

DEFAULT_ADDRESS = Address(
    label="Home", line1="12-3-45 Banjara Hills", city="Hyderabad", pincode="500034",
)


def order_request(lines, *, address: Address = DEFAULT_ADDRESS, **kw) -> CreateOrderRequest:
    return CreateOrderRequest(lines=[CartLineInput(sku=s, qty=q) for s, q in lines], address=address, **kw)


@pytest.fixture
async def agent(session):
    record = await UserRepository(session).get_or_create_by_google(
        google_sub="batch19_agent", email="batch19-agent@example.com", name="Agent",
    )
    await session.execute(
        sa_update(UserRow).where(UserRow.id == record["id"]).values(role="delivery_agent")
    )
    await session.flush()
    record["role"] = "delivery_agent"
    return record


@pytest.fixture
def delivery_repo(session) -> DeliveryRepository:
    return DeliveryRepository(session)


@pytest.fixture
def users_repo(session) -> UserRepository:
    return UserRepository(session)


@pytest.fixture
def delivery_service(delivery_repo, users_repo, order_service) -> DeliveryService:
    return DeliveryService(delivery_repo, users_repo, order_service)


# --- AAD-PERF-012: bounding_box_km ------------------------------------


def test_bounding_box_contains_a_point_within_its_radius():
    lat, lng, radius = 17.4200, 78.6000, 5.0
    lat_min, lat_max, lng_min, lng_max = bounding_box_km(lat, lng, radius)
    # A point ~3km north (inside the radius) must fall inside the box.
    near_lat = lat + (3.0 / 111.32)
    assert lat_min <= near_lat <= lat_max
    assert haversine_km(lat, lng, near_lat, lng) < radius


def test_bounding_box_excludes_a_clearly_far_point():
    lat, lng, radius = 17.4200, 78.6000, 2.0
    lat_min, lat_max, _, _ = bounding_box_km(lat, lng, radius)
    far_lat = lat + 1.0  # ~111km north — nowhere near a 2km box
    assert not (lat_min <= far_lat <= lat_max)


# --- AAD-PERF-012: list_new_requests(near=...) does the SQL-side filter --


async def test_list_new_requests_near_includes_an_order_inside_the_box(
    order_service, delivery_repo, user, milk
):
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="batch19-near-1",
    )
    candidates = await delivery_repo.list_new_requests(near=(17.4200, 78.6000, 5.0))
    assert any(c["id"] == order.id for c in candidates)


async def test_list_new_requests_near_excludes_an_order_far_outside_the_box(
    order_service, delivery_repo, user, milk
):
    far_address = Address(
        label="Home", line1="Far away street", city="Hyderabad", pincode="500034",
        latitude=17.9000, longitude=78.6000,  # well north of the box below
    )
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request(
            [("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD, address=far_address,
        ),
        idempotency_key="batch19-near-2",
    )
    candidates = await delivery_repo.list_new_requests(near=(17.4200, 78.6000, 2.0))
    assert not any(c["id"] == order.id for c in candidates)
    # The same order shows up once the box is widened enough to cover it.
    wider = await delivery_repo.list_new_requests(near=(17.4200, 78.6000, 60.0))
    assert any(c["id"] == order.id for c in wider)


async def test_list_new_requests_near_always_includes_orders_with_no_coordinates(
    order_service, delivery_repo, user, milk
):
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="batch19-near-3",
    )
    # DEFAULT_ADDRESS above carries no latitude/longitude at all.
    candidates = await delivery_repo.list_new_requests(near=(17.4200, 78.6000, 0.001))
    assert any(c["id"] == order.id for c in candidates)


async def test_list_requests_end_to_end_still_finds_a_nearby_order_via_the_sql_path(
    order_service, delivery_service, users_repo, agent, user, milk
):
    """The full DeliveryService.list_requests path, post-refactor — same
    shape as test_delivery.py's own coverage, kept here as a direct
    regression test for the SQL-filtering rewrite specifically."""
    near_address = Address(
        label="Home", line1="12-3-45 Banjara Hills", city="Hyderabad", pincode="500034",
        latitude=17.4210, longitude=78.6010,  # ~150m from the agent's own position below
    )
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request(
            [("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD, address=near_address,
        ),
        idempotency_key="batch19-e2e-1",
    )
    await users_repo.update_agent_location(agent["id"], latitude=17.4200, longitude=78.6000)
    requests = await delivery_service.list_requests(agent["id"])
    match = next(r for r in requests if r.id == order.id)
    assert match.distance_km is not None and match.distance_km < 2.0


# --- AAD-PERF-013: update_status no longer re-reads a third time --------


async def test_update_status_result_matches_a_fresh_read_after_the_fix(
    order_service, delivery_service, delivery_repo, agent, user, milk
):
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 2)], payment_method=PaymentMethod.COD),
        idempotency_key="batch19-perf013-1",
    )
    accepted = await delivery_service.accept(order.id, agent["id"])

    result = await delivery_service.update_status(order.id, agent["id"], OrderStatus.PACKED)

    assert result.status == OrderStatus.PACKED.value
    assert result.item_count == 2
    # delivery_assigned_at doesn't change on a plain status transition —
    # the value carried over from the pre-update read must still be right.
    assert result.delivery_assigned_at == accepted.delivery_assigned_at

    # Cross-check against an independent fresh read, to prove the
    # no-longer-re-read response wasn't just self-consistent by accident.
    fresh = await delivery_repo.get_one(order.id, agent["id"])
    assert fresh is not None
    assert fresh["status"] == result.status
    assert fresh["item_count"] == result.item_count
    assert fresh["delivery_assigned_at"] == result.delivery_assigned_at


async def test_update_status_calls_get_one_exactly_once_not_twice(
    order_service, delivery_service, delivery_repo, agent, user, milk, monkeypatch
):
    """AAD-PERF-013 is a pure round-trip reduction — old and new code both
    return the right answer, so the tests above can't tell them apart.
    This is the test that actually distinguishes them: before the fix,
    `update_status` called `deliveries.get_one` twice (the auth check, then
    a second time purely to build the response); after it, only the first
    call remains — the response is built from what `OrderService.
    update_status` already returned. Wrapping the real method to count
    calls, rather than asserting on timing, is what makes this a real
    regression test instead of a flaky one."""
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="batch19-perf013-2",
    )
    await delivery_service.accept(order.id, agent["id"])

    real_get_one = delivery_repo.get_one
    calls = {"n": 0}

    async def counting_get_one(order_id, agent_id):
        calls["n"] += 1
        return await real_get_one(order_id, agent_id)

    monkeypatch.setattr(delivery_service.deliveries, "get_one", counting_get_one)

    await delivery_service.update_status(order.id, agent["id"], OrderStatus.PACKED)

    assert calls["n"] == 1


# --- AAD-PERF-010: search escapes ILIKE wildcards ------------------------


async def test_search_for_a_literal_percent_does_not_match_everything(products, milk, khoya):
    results = await products.search("%")
    assert results == []


async def test_search_for_a_literal_underscore_does_not_match_any_single_character(
    products, milk, khoya
):
    results = await products.search("_")
    assert results == []


async def test_search_still_matches_an_ordinary_substring(products, milk):
    results = await products.search("cow")
    assert any(p.slug == "full-cream-cow-milk" for p in results)
