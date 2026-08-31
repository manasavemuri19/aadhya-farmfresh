from __future__ import annotations

from app.core.errors import Conflict
from app.domain.geo import haversine_km
from app.repositories.delivery import DeliveryRepository
from app.repositories.users import UserRepository
from app.schemas.delivery import DeliveryOrderView

# Widened one km at a time until someone could plausibly take the job, or
# until widening further stops being a delivery any agent would reasonably
# make — at which point it's better to show the order (distance-labelled)
# than to hide a paid order from every agent forever.
_START_RADIUS_KM = 2.0
_RADIUS_STEP_KM = 1.0
_MAX_RADIUS_KM = 15.0


class DeliveryService:
    def __init__(self, deliveries: DeliveryRepository, users: UserRepository) -> None:
        self.deliveries = deliveries
        self.users = users

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

    async def update_location(self, agent_id: str, *, latitude: float, longitude: float) -> None:
        await self.users.update_agent_location(agent_id, latitude=latitude, longitude=longitude)
