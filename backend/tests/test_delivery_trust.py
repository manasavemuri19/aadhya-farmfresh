"""Batch 4d — the delivery trust model.

Four independent findings, one shared theme: an agent's own app is not a
trusted boundary, and neither is any single agent's word about where they
are or how many jobs they're already carrying.

AAD-REL-006 (reassign): staff can move an order to a different agent, or
back to the pool, regardless of who currently holds it — as long as there's
still something to reassign.
AAD-SEC-029 (concurrent-order cap): one agent can't sit on an unbounded
number of accepted orders at once.
AAD-SEC-030 (leaner pre-accept view): the list of not-yet-accepted orders
shows no address, no notes, no order value — and no longer floods an
unlocated agent with every waiting order at once.
AAD-SEC-028 (location plausibility): an agent-reported GPS position is
rejected outright if it's nowhere near the farm's operating area, and
flagged (not rejected) if the implied speed from the last reading is
physically impossible.
"""

from __future__ import annotations

import typing
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import update

import app.services.delivery_service as delivery_service_module
from app.api.deps import Principal, require_staff
from app.api.v1.routes.admin import reassign_delivery
from app.core.errors import Conflict, NotFound, ValidationError
from app.db.models import User as UserRow
from app.domain.enums import OrderStatus, PaymentMethod
from app.repositories.delivery import DeliveryRepository
from app.repositories.users import UserRepository
from app.schemas.auth import Address
from app.schemas.delivery import AgentLocationUpdate, ReassignDeliveryRequest
from app.schemas.order import CartLineInput, CreateOrderRequest
from app.services.delivery_service import _MAX_CONCURRENT_ORDERS, DeliveryService

DEFAULT_ADDRESS = Address(
    label="Home", line1="12-3-45 Banjara Hills", city="Hyderabad", pincode="500034",
)

# Well inside HYDERABAD_LAT_RANGE / HYDERABAD_LNG_RANGE (app/domain/geo.py).
HYDERABAD_POINT = (17.4200, 78.6000)
# Nowhere near it — New York.
OUTSIDE_POINT = (40.7128, -74.0060)


def order_request(lines, *, address: Address = DEFAULT_ADDRESS, **kw) -> CreateOrderRequest:
    return CreateOrderRequest(
        lines=[CartLineInput(sku=s, qty=q) for s, q in lines],
        address=address,
        **kw,
    )


@pytest.fixture
async def agent(session):
    record = await UserRepository(session).get_or_create_by_google(
        google_sub="test_agent_sub_0001", email="agent@example.com", name="Test Agent",
    )
    await session.execute(
        update(UserRow).where(UserRow.id == record["id"]).values(role="delivery_agent")
    )
    await session.flush()
    record["role"] = "delivery_agent"
    return record


@pytest.fixture
async def agent2(session):
    """A second delivery agent, for reassign-between-agents tests."""
    record = await UserRepository(session).get_or_create_by_google(
        google_sub="test_agent_sub_0002", email="agent2@example.com", name="Test Agent 2",
    )
    await session.execute(
        update(UserRow).where(UserRow.id == record["id"]).values(role="delivery_agent")
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


async def _make_order(order_service, user, sku, key, **kw):
    return await order_service.create_order(
        user_id=user["id"],
        request=order_request([(sku, 1)], payment_method=PaymentMethod.COD, **kw),
        idempotency_key=key,
    )


# ---------- AAD-REL-006: staff reassign ----------


async def test_reassign_moves_order_to_a_different_agent(
    order_service, delivery_service, agent, agent2, user, milk
):
    order = await _make_order(order_service, user, "MILK-COW-1L", "reassign-1")
    await delivery_service.accept(order.id, agent["id"])

    await delivery_service.reassign(order.id, new_agent_id=agent2["id"], actor_id="usr_staff1")

    assert not any(o.id == order.id for o in await delivery_service.list_ongoing(agent["id"]))
    assert any(o.id == order.id for o in await delivery_service.list_ongoing(agent2["id"]))


async def test_reassign_to_none_sends_it_back_to_the_pool(
    order_service, delivery_service, agent, user, milk
):
    order = await _make_order(order_service, user, "MILK-COW-1L", "reassign-2")
    await delivery_service.accept(order.id, agent["id"])

    await delivery_service.reassign(order.id, new_agent_id=None, actor_id="usr_staff1")

    assert not any(o.id == order.id for o in await delivery_service.list_ongoing(agent["id"]))
    assert any(r.id == order.id for r in await delivery_service.list_requests(agent["id"]))


async def test_reassign_rejects_a_target_who_is_not_a_delivery_agent(
    order_service, delivery_service, agent, user, milk
):
    order = await _make_order(order_service, user, "MILK-COW-1L", "reassign-3")
    await delivery_service.accept(order.id, agent["id"])

    with pytest.raises(ValidationError):
        await delivery_service.reassign(order.id, new_agent_id=user["id"], actor_id="usr_staff1")


async def test_reassign_rejects_orders_with_nothing_left_to_reassign(
    order_service, delivery_service, agent, agent2, user, milk
):
    """Once an order is DELIVERED (or cancelled/refunded), it's done — a
    reassign at that point isn't a correction, it's a bug in whatever's
    calling it."""
    order = await _make_order(order_service, user, "MILK-COW-1L", "reassign-4")
    await delivery_service.accept(order.id, agent["id"])
    await delivery_service.update_status(order.id, agent["id"], OrderStatus.PACKED)
    await delivery_service.update_status(order.id, agent["id"], OrderStatus.OUT_FOR_DELIVERY)
    await delivery_service.update_status(order.id, agent["id"], OrderStatus.DELIVERED)

    with pytest.raises(NotFound):
        await delivery_service.reassign(order.id, new_agent_id=agent2["id"], actor_id="usr_staff1")


async def test_reassign_route_is_staff_only():
    """A regression here — swapping require_staff for something laxer, or
    dropping the dependency entirely — would silently let any signed-in
    account move deliveries between agents. Same technique as
    test_set_price_route_requires_admin_not_just_staff (test_stock_admin_
    integrity.py): inspect the actual Depends() wiring, since a direct
    Python call bypasses it."""
    hints = typing.get_type_hints(reassign_delivery, include_extras=True)
    dependency = hints["staff"].__metadata__[0].dependency
    assert dependency is require_staff


async def test_reassign_route_delegates_to_the_service(
    order_service, delivery_service, agent, agent2, user, milk
):
    order = await _make_order(order_service, user, "MILK-COW-1L", "reassign-5")
    await delivery_service.accept(order.id, agent["id"])

    await reassign_delivery(
        order.id,
        ReassignDeliveryRequest(agent_id=agent2["id"]),
        Principal("usr_staff1", "staff"),
        delivery_service,
    )

    assert any(o.id == order.id for o in await delivery_service.list_ongoing(agent2["id"]))


# ---------- AAD-SEC-029: concurrent-order cap ----------


async def test_accept_enforces_the_concurrent_order_cap(
    order_service, delivery_service, agent, user, khoya
):
    orders = [
        await _make_order(order_service, user, "KHOYA-250G", f"cap-{i}")
        for i in range(_MAX_CONCURRENT_ORDERS + 1)
    ]
    for order in orders[:_MAX_CONCURRENT_ORDERS]:
        await delivery_service.accept(order.id, agent["id"])

    with pytest.raises(Conflict):
        await delivery_service.accept(orders[_MAX_CONCURRENT_ORDERS].id, agent["id"])


async def test_releasing_one_makes_room_under_the_cap(
    order_service, delivery_service, agent, user, khoya
):
    orders = [
        await _make_order(order_service, user, "KHOYA-250G", f"cap-release-{i}")
        for i in range(_MAX_CONCURRENT_ORDERS + 1)
    ]
    for order in orders[:_MAX_CONCURRENT_ORDERS]:
        await delivery_service.accept(order.id, agent["id"])

    await delivery_service.release(orders[0].id, agent["id"])
    # Now back under the cap — the same order that just failed above should
    # succeed.
    accepted = await delivery_service.accept(orders[_MAX_CONCURRENT_ORDERS].id, agent["id"])
    assert accepted.id == orders[_MAX_CONCURRENT_ORDERS].id


async def test_cap_is_per_agent_not_global(
    order_service, delivery_service, agent, agent2, user, khoya
):
    orders = [
        await _make_order(order_service, user, "KHOYA-250G", f"cap-peragent-{i}")
        for i in range(_MAX_CONCURRENT_ORDERS + 1)
    ]
    for order in orders[:_MAX_CONCURRENT_ORDERS]:
        await delivery_service.accept(order.id, agent["id"])

    # agent is now at the cap; agent2 has zero, so this must succeed.
    accepted = await delivery_service.accept(orders[_MAX_CONCURRENT_ORDERS].id, agent2["id"])
    assert accepted.id == orders[_MAX_CONCURRENT_ORDERS].id


# ---------- AAD-SEC-030: leaner pre-accept view ----------


async def test_request_view_carries_no_address_notes_or_order_value(
    order_service, delivery_service, agent, user, milk
):
    order = await _make_order(order_service, user, "MILK-COW-1L", "lean-1")
    match = next(r for r in await delivery_service.list_requests(agent["id"]) if r.id == order.id)

    assert not hasattr(match, "address")
    assert not hasattr(match, "notes")
    assert not hasattr(match, "total_paise")


async def test_unlocated_requests_are_capped_not_unfiltered(
    monkeypatch, order_service, delivery_service, agent, user, milk
):
    """Without a fallback cap, an agent who hasn't shared a location yet
    would see every paid-and-waiting order in the farm at once. Patches the
    module constant down to 3 so the test doesn't need to create 20+ real
    orders to exercise it."""
    monkeypatch.setattr(delivery_service_module, "_UNLOCATED_FALLBACK_LIMIT", 3)

    orders = [
        await _make_order(order_service, user, "MILK-COW-1L", f"unlocated-{i}")
        for i in range(5)
    ]

    requests = await delivery_service.list_requests(agent["id"])
    assert len(requests) == 3
    # Oldest-first: the first three created, not an arbitrary three.
    assert [r.id for r in requests] == [o.id for o in orders[:3]]


# ---------- AAD-SEC-028: agent location plausibility ----------


def test_out_of_bounds_location_is_rejected():
    lat, lng = OUTSIDE_POINT
    with pytest.raises(PydanticValidationError):
        AgentLocationUpdate(latitude=lat, longitude=lng)


def test_in_bounds_location_is_accepted():
    lat, lng = HYDERABAD_POINT
    AgentLocationUpdate(latitude=lat, longitude=lng)  # must not raise


async def test_implausible_jump_is_flagged_but_still_stored(session, users_repo, agent):
    await users_repo.update_agent_location(agent["id"], latitude=17.4200, longitude=78.6000)
    # Back-date the reading so the next update's implied speed is
    # computable and clearly over the threshold, without a real 5-second
    # sleep in the test.
    await session.execute(
        update(UserRow)
        .where(UserRow.id == agent["id"])
        .values(last_location_at=datetime.now(UTC) - timedelta(seconds=10))
    )
    await session.flush()

    # ~55km away in 10 seconds — no delivery vehicle does that.
    flagged = await users_repo.update_agent_location(
        agent["id"], latitude=17.9000, longitude=78.6000
    )

    assert flagged is True
    location = await users_repo.get_agent_location(agent["id"])
    assert location == (17.9000, 78.6000)  # still stored, not rejected


async def test_plausible_move_is_not_flagged(session, users_repo, agent):
    await users_repo.update_agent_location(agent["id"], latitude=17.4200, longitude=78.6000)
    await session.execute(
        update(UserRow)
        .where(UserRow.id == agent["id"])
        .values(last_location_at=datetime.now(UTC) - timedelta(seconds=10))
    )
    await session.flush()

    # A couple hundred metres away in 10 seconds — an ordinary update.
    flagged = await users_repo.update_agent_location(
        agent["id"], latitude=17.4210, longitude=78.6010
    )
    assert flagged is False


async def test_rapid_updates_below_the_interval_floor_are_not_flagged(users_repo, agent):
    """Two updates seconds (not milliseconds) apart in real wall-clock time
    would otherwise imply an enormous, meaningless speed purely from the
    tiny interval — _MIN_CHECK_INTERVAL_SECONDS exists to skip the check
    rather than flag on that noise."""
    await users_repo.update_agent_location(agent["id"], latitude=17.4200, longitude=78.6000)
    flagged = await users_repo.update_agent_location(
        agent["id"], latitude=17.9000, longitude=78.6000
    )
    assert flagged is False


async def test_first_ever_location_report_is_never_flagged(users_repo, agent):
    """No previous reading to compare against — nothing to be implausible
    relative to."""
    flagged = await users_repo.update_agent_location(
        agent["id"], latitude=17.4200, longitude=78.6000
    )
    assert flagged is False
