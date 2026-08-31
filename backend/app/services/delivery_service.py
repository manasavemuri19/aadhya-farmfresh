from __future__ import annotations

from app.core.errors import Conflict, Forbidden
from app.domain.enums import OrderStatus
from app.domain.geo import haversine_km
from app.repositories.delivery import DeliveryRepository
from app.repositories.users import UserRepository
from app.schemas.delivery import DeliveryOrderView
from app.services.order_service import OrderService

# Widened one km at a time until someone could plausibly take the job, or
# until widening further stops being a delivery any agent would reasonably
# make — at which point it's better to show the order (distance-labelled)
# than to hide a paid order from every agent forever.
_START_RADIUS_KM = 2.0
_RADIUS_STEP_KM = 1.0
_MAX_RADIUS_KM = 15.0

# The only statuses an agent can move an order into themselves. Confirming
# (that's payment), cancelling and refunding stay staff/admin-only via
# /admin/orders/{id}/status — an agent's whole write surface on an order's
# status is "I packed it, I'm carrying it, I dropped it off."
_AGENT_ALLOWED_STATUSES = frozenset(
    {OrderStatus.PACKED, OrderStatus.OUT_FOR_DELIVERY, OrderStatus.DELIVERED}
)

_DEFAULT_STATUS_NOTE: dict[OrderStatus, str] = {
    OrderStatus.PACKED: "Packed by delivery agent",
    OrderStatus.OUT_FOR_DELIVERY: "Picked up, on the way",
    OrderStatus.DELIVERED: "Delivered",
}


class DeliveryService:
    def __init__(
        self, deliveries: DeliveryRepository, users: UserRepository, orders: OrderService
    ) -> None:
        self.deliveries = deliveries
        self.users = users
        self.orders = orders

    async def list_requests(self, agent_id: str) -> list[DeliveryOrderView]:
        candidates = await self.deliveries.list_new_requests()
        agent_location = await self.users.get_agent_location(agent_id)

        located: list[tuple[dict, float]] = []
        unlocated: list[dict] = []
        for order in candidates:
            lat = order["address"].get("latitude")
            lng = order["address"].get("longitude")
            if agent_location is not None and lat is not None and lng is not None:
                distance = haversine_km(*agent_location, lat, lng)
                located.append((order, distance))
            else:
                # Either the agent hasn't shared a location yet, or this
                # particular order's address has no coordinates on it (an
                # old order, or a hand-typed address). Either way there's
                # nothing to measure — it goes out unfiltered rather than
                # silently never reaching anyone.
                unlocated.append(order)
        located.sort(key=lambda pair: pair[1])

        if agent_location is not None:
            radius = _START_RADIUS_KM
            within = [pair for pair in located if pair[1] <= radius]
            while not within and located and radius < _MAX_RADIUS_KM:
                radius += _RADIUS_STEP_KM
                within = [pair for pair in located if pair[1] <= radius]
            located = within

        ordered: list[tuple[dict, float | None]] = [
            (order, distance) for order, distance in located
        ] + [(order, None) for order in unlocated]

        return [
            DeliveryOrderView(distance_km=round(distance, 1) if distance is not None else None, **order)
            for order, distance in ordered
        ]

    async def list_ongoing(self, agent_id: str) -> list[DeliveryOrderView]:
        orders = await self.deliveries.list_ongoing(agent_id)
        return [DeliveryOrderView(distance_km=None, **order) for order in orders]

    async def accept(self, order_id: str, agent_id: str) -> DeliveryOrderView:
        order = await self.deliveries.accept(order_id, agent_id)
        if order is None:
            raise Conflict("This order has already been accepted, or is no longer available.")
        return DeliveryOrderView(distance_km=None, **order)

    async def release(self, order_id: str, agent_id: str) -> None:
        """Let an agent back out of an order they accepted, sending it back
        to the pool for anyone else to pick up (see DeliveryRepository.release
        for exactly which window this is allowed in)."""
        released = await self.deliveries.release(order_id, agent_id)
        if not released:
            raise Conflict(
                "This order can no longer be released — it may already be packed for "
                "pickup, or it isn't currently assigned to you."
            )

    async def update_status(
        self, order_id: str, agent_id: str, new_status: OrderStatus, note: str = ""
    ) -> DeliveryOrderView:
        """Mark an order Packed, On the way, or Delivered.

        Two checks happen before this ever touches the order: the target
        status has to be one an agent is allowed to set at all, and the
        order has to actually be assigned to *this* agent — `get_one`
        returning None covers both "no such order" and "not yours" the same
        way, so this never leaks which one it was. The actual transition
        (including the legal-sequence check — no jumping straight to
        Delivered from Confirmed) is delegated to OrderService, which is
        already the one place that logic lives.
        """
        if new_status not in _AGENT_ALLOWED_STATUSES:
            raise Forbidden("Delivery agents can only mark an order Packed, On the way, or Delivered.")

        existing = await self.deliveries.get_one(order_id, agent_id)
        if existing is None:
            raise Forbidden("This order isn't assigned to you.")

        await self.orders.update_status(
            order_id=order_id,
            new_status=new_status,
            note=note or _DEFAULT_STATUS_NOTE[new_status],
            actor=agent_id,
        )

        updated = await self.deliveries.get_one(order_id, agent_id)
        assert updated is not None
        return DeliveryOrderView(distance_km=None, **updated)

    async def update_location(self, agent_id: str, *, latitude: float, longitude: float) -> None:
        await self.users.update_agent_location(agent_id, latitude=latitude, longitude=longitude)
