"""Delivery-agent matching and the atomic accept — the two things worth
locking in: an order that isn't paid-and-unassigned never shows up as a
request, and two attempts to accept the same order can't both win.
"""

from __future__ import annotations

import pytest
from sqlalchemy import update

from app.core.errors import Conflict, Forbidden
from app.db.models import User as UserRow
from app.domain.enums import OrderStatus, PaymentMethod
from app.repositories.delivery import DeliveryRepository
from app.repositories.users import UserRepository
from app.schemas.auth import Address
from app.schemas.order import CartLineInput, CreateOrderRequest
from app.services.delivery_service import DeliveryService

DEFAULT_ADDRESS = Address(
    label="Home", line1="12-3-45 Banjara Hills", city="Hyderabad", pincode="500034",
)


def order_request(lines, *, address: Address = DEFAULT_ADDRESS, **kw) -> CreateOrderRequest:
    return CreateOrderRequest(
        lines=[CartLineInput(sku=s, qty=q) for s, q in lines],
        address=address,
        **kw,
    )


@pytest.fixture
async def agent(session):
    """A second account, promoted to delivery_agent directly — matching how
    this role is actually assigned in production: never self-serve, the
    same way staff/admin accounts are set up."""
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
def delivery_repo(session) -> DeliveryRepository:
    return DeliveryRepository(session)


@pytest.fixture
def users_repo(session) -> UserRepository:
    return UserRepository(session)


@pytest.fixture
def delivery_service(delivery_repo, users_repo, order_service) -> DeliveryService:
    # Stale since DeliveryService.update_status started delegating the
    # actual transition to OrderService (see delivery_service.py) — this
    # fixture was never updated to pass it, so every test in this file has
    # been erroring at setup, not failing on its own merits. Fixed here
    # rather than left as baseline noise, since Batch 4d's own new tests
    # need a working fixture in the same file.
    return DeliveryService(delivery_repo, users_repo, order_service)


async def test_confirmed_unassigned_order_is_a_request(
    order_service, delivery_service, agent, user, milk
):
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="delivery-test-1",
    )
    requests = await delivery_service.list_requests(agent["id"])
    assert any(r.id == order.id for r in requests)


async def test_accept_is_atomic_second_attempt_conflicts(
    order_service, delivery_service, agent, user, milk
):
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="delivery-test-2",
    )
    first = await delivery_service.accept(order.id, agent["id"])
    assert first.id == order.id

    with pytest.raises(Conflict):
        await delivery_service.accept(order.id, agent["id"])


async def test_accepted_order_moves_from_requests_to_ongoing(
    order_service, delivery_service, agent, user, milk
):
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="delivery-test-3",
    )
    await delivery_service.accept(order.id, agent["id"])

    requests = await delivery_service.list_requests(agent["id"])
    assert not any(r.id == order.id for r in requests)

    ongoing = await delivery_service.list_ongoing(agent["id"])
    assert any(o.id == order.id for o in ongoing)


async def test_order_with_no_address_coordinates_still_shows_as_a_request(
    order_service, delivery_service, users_repo, agent, user, milk
):
    """The safety net: an address with no lat/long (an old order, or one
    typed by hand) must never silently vanish from every agent's list."""
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="delivery-test-4",
    )
    await users_repo.update_agent_location(agent["id"], latitude=17.4200, longitude=78.6000)

    requests = await delivery_service.list_requests(agent["id"])
    match = next(r for r in requests if r.id == order.id)
    assert match.distance_km is None


async def test_expanding_radius_widens_until_something_is_in_range(
    order_service, delivery_service, users_repo, agent, user, milk
):
    far_address = Address(
        label="Home", line1="Far away street", city="Hyderabad", pincode="500034",
        latitude=17.5000, longitude=78.6000,
    )
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request(
            [("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD, address=far_address,
        ),
        idempotency_key="delivery-test-5",
    )
    # ~9km from the order — outside the starting 2km radius, so matching
    # only succeeds if the radius actually widens.
    await users_repo.update_agent_location(agent["id"], latitude=17.4200, longitude=78.6000)

    requests = await delivery_service.list_requests(agent["id"])
    match = next(r for r in requests if r.id == order.id)
    assert match.distance_km is not None and match.distance_km > 2.0


# --- AAD-OPS-018: release, update_status authorization, the status allowlist


async def test_release_sends_an_accepted_order_back_to_the_pool(
    order_service, delivery_service, agent, user, milk
):
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="delivery-test-release-1",
    )
    await delivery_service.accept(order.id, agent["id"])
    ongoing = await delivery_service.list_ongoing(agent["id"])
    assert any(o.id == order.id for o in ongoing)

    await delivery_service.release(order.id, agent["id"])

    ongoing_after = await delivery_service.list_ongoing(agent["id"])
    assert not any(o.id == order.id for o in ongoing_after)
    requests_after = await delivery_service.list_requests(agent["id"])
    assert any(r.id == order.id for r in requests_after)


async def test_release_conflicts_when_the_order_is_not_assigned_to_this_agent(
    order_service, delivery_service, agent, user, milk
):
    """An order nobody has accepted yet — the same `release()` call an
    agent who mistakenly thinks they hold it would make."""
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="delivery-test-release-2",
    )

    with pytest.raises(Conflict):
        await delivery_service.release(order.id, agent["id"])


async def test_release_conflicts_when_accepted_by_a_different_agent(
    order_service, delivery_service, users_repo, agent, user, milk
):
    """The authorization half specifically: a *second* agent trying to
    release an order the *first* agent holds must not be able to — same
    underlying check (`DeliveryRepository.release`'s own `agent_id` match),
    different scenario than "nobody has it yet"."""
    other_agent = await users_repo.get_or_create_by_google(
        google_sub="test_agent_sub_0002", email="agent2@example.com", name="Other Agent",
    )
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="delivery-test-release-3",
    )
    await delivery_service.accept(order.id, agent["id"])

    with pytest.raises(Conflict):
        await delivery_service.release(order.id, other_agent["id"])


async def test_update_status_is_forbidden_when_the_order_is_not_assigned_to_this_agent(
    order_service, delivery_service, agent, user, milk
):
    """`get_one` returning `None` covers both "no such order" and "not
    yours" the same way, by design (the docstring is explicit about not
    leaking which one it was) — this is the "not yours" half: nobody has
    accepted this order at all yet."""
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="delivery-test-status-1",
    )

    with pytest.raises(Forbidden):
        await delivery_service.update_status(order.id, agent["id"], OrderStatus.PACKED)


async def test_update_status_is_forbidden_for_a_status_agents_cannot_set(
    order_service, delivery_service, agent, user, milk
):
    """The allowlist itself: agents can only move an order to Packed, On
    the way, or Delivered — never back to Confirmed, and never straight to
    Cancelled or Refunded, both of which stay staff/admin-only."""
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="delivery-test-status-2",
    )
    await delivery_service.accept(order.id, agent["id"])

    with pytest.raises(Forbidden):
        await delivery_service.update_status(order.id, agent["id"], OrderStatus.CANCELLED)


async def test_update_status_happy_path_moves_the_order_and_preserves_assignment_time(
    order_service, delivery_service, agent, user, milk
):
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="delivery-test-status-3",
    )
    accepted = await delivery_service.accept(order.id, agent["id"])

    packed = await delivery_service.update_status(order.id, agent["id"], OrderStatus.PACKED)
    assert packed.status == OrderStatus.PACKED.value
    # AAD-PERF-013: this comes from the pre-update `existing` read, not a
    # third re-read of the order — pinned here so a future regression in
    # that fix would show up as a wrong (or missing) value, not just an
    # extra query.
    assert packed.delivery_assigned_at == accepted.delivery_assigned_at

    on_the_way = await delivery_service.update_status(
        order.id, agent["id"], OrderStatus.OUT_FOR_DELIVERY
    )
    assert on_the_way.status == OrderStatus.OUT_FOR_DELIVERY.value

    # AAD-SEC-027: an agent can no longer self-report DELIVERED through the
    # plain status endpoint — see _AGENT_ALLOWED_STATUSES' own comment. The
    # code the customer's app would show them is read back via the
    # customer-facing OrderView (DeliveryOrderView deliberately never
    # carries it), then entered through verify_delivery.
    customer_view = await order_service.get_for_user(order.id, user["id"])
    assert customer_view.delivery_code is not None
    delivered = await delivery_service.verify_delivery(
        order.id, agent["id"], customer_view.delivery_code
    )
    assert delivered.status == OrderStatus.DELIVERED.value
