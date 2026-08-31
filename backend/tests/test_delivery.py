"""Delivery-agent matching and the atomic accept — the two things worth
locking in: an order that isn't paid-and-unassigned never shows up as a
request, and two attempts to accept the same order can't both win.
"""

from __future__ import annotations

import pytest
from sqlalchemy import update

from app.core.errors import Conflict
from app.db.models import User as UserRow
from app.domain.enums import PaymentMethod
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
def delivery_service(delivery_repo, users_repo) -> DeliveryService:
    return DeliveryService(delivery_repo, users_repo)


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
