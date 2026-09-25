from __future__ import annotations

import logging

from app.core.errors import Conflict, Forbidden, NotFound, ValidationError
from app.domain.enums import OrderStatus, Role
from app.domain.geo import haversine_km
from app.repositories.delivery import DeliveryRepository
from app.repositories.users import UserRepository
from app.schemas.delivery import DeliveryOrderView, DeliveryRequestView
from app.services.order_service import OrderService

# Widened one km at a time until someone could plausibly take the job, or
# until widening further stops being a delivery any agent would reasonably
# make — at which point it's better to show the order (distance-labelled)
# than to hide a paid order from every agent forever.
_START_RADIUS_KM = 2.0
_RADIUS_STEP_KM = 1.0
_MAX_RADIUS_KM = 15.0

# AAD-SEC-030: when an agent hasn't shared a location yet, there's nothing to
# sort or radius-filter by — but "everything, unfiltered" used to mean every
# paid-and-waiting order in the farm handed to one agent's screen at once.
# Oldest-first (list_new_requests' own ordering) plus a cap still gets a
# just-opened-the-app agent useful work without that.
_UNLOCATED_FALLBACK_LIMIT = 20

# AAD-SEC-029: one agent holding an unbounded number of orders "in flight"
# at once is either a stuck/abandoned batch or a way to sit on every order
# in the queue without delivering any of them. 5 is generous for a single
# scooter/bike run around a small delivery radius — comfortably above what
# one trip normally carries — while still being an actual limit rather than
# a number nobody could ever hit.
_MAX_CONCURRENT_ORDERS = 5

log = logging.getLogger(__name__)

# The only statuses an agent can move an order into themselves via the plain
# status endpoint. Confirming (that's payment), cancelling and refunding
# stay staff/admin-only via /admin/orders/{id}/status. AAD-SEC-027: DELIVERED
# was removed from here — an agent can no longer self-report delivery with
# no evidence; it's reached only through verify_delivery below (entering the
# customer's in-app code), or by a staff override through /admin.
_AGENT_ALLOWED_STATUSES = frozenset({OrderStatus.PACKED, OrderStatus.OUT_FOR_DELIVERY})

_DEFAULT_STATUS_NOTE: dict[OrderStatus, str] = {
    OrderStatus.PACKED: "Packed by delivery agent",
    OrderStatus.OUT_FOR_DELIVERY: "Picked up, on the way",
}


class DeliveryService:
    def __init__(
        self, deliveries: DeliveryRepository, users: UserRepository, orders: OrderService
    ) -> None:
        self.deliveries = deliveries
        self.users = users
        self.orders = orders

    async def list_requests(self, agent_id: str) -> list[DeliveryRequestView]:
        """AAD-SEC-030: this is the pre-accept list — every agent sees every
        not-yet-taken order here, so it deliberately returns the lean
        DeliveryRequestView (no address, no notes, no order value) rather
        than the full DeliveryOrderView. An agent gets the full picture only
        for orders they've actually accepted (list_ongoing, accept).

        AAD-PERF-012: used to load every pending order (up to the hard
        `limit=100`) unconditionally, then do all the radius filtering and
        widening in Python over that fully materialised list — a load-then-
        discard on every single poll, from every agent, whether or not
        anything was actually near them. `list_new_requests`'s own `near`
        parameter now does the coarse filtering in SQL (a cheap bounding
        box), so each radius step only loads the orders that could
        plausibly be in range at that radius, plus whatever has no
        coordinates at all (those always need to be seen regardless of
        radius — see the loop below). `haversine_km` still does the exact
        distance check and the final sort; the box is only ever a filter,
        never the answer.
        """
        agent_location = await self.users.get_agent_location(agent_id)

        if agent_location is None:
            # Nothing to measure distance against — same fallback as
            # before: oldest-first, capped, no radius logic at all.
            candidates = await self.deliveries.list_new_requests()
            unlocated = candidates[:_UNLOCATED_FALLBACK_LIMIT]
            ordered: list[tuple[dict, float | None]] = [(order, None) for order in unlocated]
        else:
            lat, lng = agent_location
            located: list[tuple[dict, float]] = []
            unlocated_by_id: dict[str, dict] = {}
            radius = _START_RADIUS_KM
            while True:
                candidates = await self.deliveries.list_new_requests(near=(lat, lng, radius))
                located = []
                for order in candidates:
                    o_lat = order["address"].get("latitude")
                    o_lng = order["address"].get("longitude")
                    if o_lat is None or o_lng is None:
                        # However this order's own coordinates look, it's
                        # in every widening step's result set (the `near`
                        # filter always passes coordinate-less orders
                        # through) — collect it once, not once per radius.
                        unlocated_by_id[order["id"]] = order
                        continue
                    distance = haversine_km(lat, lng, o_lat, o_lng)
                    if distance <= radius:
                        located.append((order, distance))
                if located or radius >= _MAX_RADIUS_KM:
                    break
                radius += _RADIUS_STEP_KM
            located.sort(key=lambda pair: pair[1])

            ordered = [(order, distance) for order, distance in located] + [
                (order, None) for order in unlocated_by_id.values()
            ]

        # DeliveryRequestView is deliberately narrower than the dict
        # list_new_requests returns (Schema forbids extra fields, so the
        # full dict can't just be splatted in here) — picked explicitly
        # rather than widening the view to match.
        return [
            DeliveryRequestView(
                id=order["id"],
                order_number=order["order_number"],
                item_count=order["item_count"],
                created_at=order["created_at"],
                distance_km=round(distance, 1) if distance is not None else None,
            )
            for order, distance in ordered
        ]

    async def list_ongoing(self, agent_id: str) -> list[DeliveryOrderView]:
        orders = await self.deliveries.list_ongoing(agent_id)
        return [DeliveryOrderView(distance_km=None, **order) for order in orders]

    async def accept(self, order_id: str, agent_id: str) -> DeliveryOrderView:
        # AAD-SEC-029: checked before the CAS, not after — no point taking
        # the compare-and-swap on the order row just to throw the result
        # away. This is a live count each time (not a cached counter), so
        # it's always the agent's real current load, including anything a
        # staff reassign (see `reassign`) may have just added to it.
        ongoing_count = await self.deliveries.count_ongoing(agent_id)
        if ongoing_count >= _MAX_CONCURRENT_ORDERS:
            raise Conflict(
                f"You already have {ongoing_count} orders in progress — finish or release "
                "one before accepting another."
            )

        order = await self.deliveries.accept(order_id, agent_id)
        if order is None:
            raise Conflict("This order has already been accepted, or is no longer available.")
        return DeliveryOrderView(distance_km=None, **order)

    async def reassign(
        self, order_id: str, *, new_agent_id: str | None, actor_id: str, note: str = ""
    ) -> None:
        """AAD-REL-006: staff move an order to a different agent (or back to
        the unassigned pool with new_agent_id=None) — an agent's stuck
        vehicle, a no-show, a shift change. Unlike `accept`, this is a
        staff action and deliberately does not enforce
        _MAX_CONCURRENT_ORDERS: that cap exists to stop an agent from
        self-serve hoarding orders, not to block a farm staff member from
        handing someone an order in an emergency.

        No OrderEvent is recorded: OrderEvent.status is CHECK-constrained to
        real OrderStatus values (ck_order_event_status_valid, Batch 4b), and
        a reassignment doesn't change the order's status — there's no valid
        status to attach a synthetic event to without either loosening that
        constraint or recording a misleading one. This logs instead, same
        as the location-jump flag (AAD-SEC-028) — visible to anyone
        reviewing logs, without overloading a status-transition table with
        an event that isn't one.
        """
        if new_agent_id is not None:
            agent = await self.users.get_by_id(new_agent_id)
            if agent is None or agent.get("role") != Role.DELIVERY_AGENT.value:
                raise ValidationError("new_agent_id must be an existing delivery agent.")

        ok = await self.deliveries.reassign(order_id, new_agent_id=new_agent_id)
        if not ok:
            raise NotFound(
                "No such order, or it's already delivered/cancelled/refunded and has "
                "nothing left to reassign."
            )
        log.info(
            "delivery_order_reassigned",
            extra={
                "order_id": order_id,
                "new_agent_id": new_agent_id,
                "actor_id": actor_id,
                "note": note,
            },
        )

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

        AAD-PERF-013: used to re-read this same order a third time, via
        `deliveries.get_one`, purely to build the response — on top of the
        `existing` read just above and whatever `OrderService.update_status`
        itself reads internally (a pre-transition read and a post-
        transition one). `OrderService.update_status` already returns a
        full `OrderView` of the very row this just changed; that's reused
        directly to build the `DeliveryOrderView` instead of asking the
        database for the same order a third time. `delivery_assigned_at`
        isn't on `OrderView` at all (it's an agent-assignment detail, not a
        customer-facing one) — a plain status transition never touches it,
        so the value already in hand from `existing` is still correct.
        """
        if new_status not in _AGENT_ALLOWED_STATUSES:
            raise Forbidden(
                "Delivery agents can only mark an order Packed, On the way, or Delivered."
            )

        existing = await self.deliveries.get_one(order_id, agent_id)
        if existing is None:
            raise Forbidden("This order isn't assigned to you.")

        updated_view = await self.orders.update_status(
            order_id=order_id,
            new_status=new_status,
            note=note or _DEFAULT_STATUS_NOTE[new_status],
            actor=agent_id,
        )

        return DeliveryOrderView(
            id=updated_view.id,
            order_number=updated_view.order_number,
            status=updated_view.status.value,
            address=updated_view.address,
            notes=updated_view.notes,
            total_paise=updated_view.total_paise,
            item_count=sum(line.qty for line in updated_view.lines),
            created_at=updated_view.created_at,
            delivery_assigned_at=existing["delivery_assigned_at"],
            distance_km=None,
        )

    async def verify_delivery(
        self, order_id: str, agent_id: str, code: str
    ) -> DeliveryOrderView:
        """AAD-SEC-027: the agent-facing half of in-app proof-of-delivery —
        the only way an order still assigned to an agent reaches DELIVERED
        through this router (see _AGENT_ALLOWED_STATUSES' own comment).

        Same ownership check as update_status, and for the same reason: a
        wrong-order guess or a stolen agent session shouldn't be able to
        probe another agent's delivery codes by order id alone.
        """
        existing = await self.deliveries.get_one(order_id, agent_id)
        if existing is None:
            raise Forbidden("This order isn't assigned to you.")

        updated_view = await self.orders.verify_delivery_code(
            order_id=order_id, code=code, actor=agent_id
        )

        return DeliveryOrderView(
            id=updated_view.id,
            order_number=updated_view.order_number,
            status=updated_view.status.value,
            address=updated_view.address,
            notes=updated_view.notes,
            total_paise=updated_view.total_paise,
            item_count=sum(line.qty for line in updated_view.lines),
            created_at=updated_view.created_at,
            delivery_assigned_at=existing["delivery_assigned_at"],
            distance_km=None,
        )

    async def update_location(self, agent_id: str, *, latitude: float, longitude: float) -> None:
        await self.users.update_agent_location(agent_id, latitude=latitude, longitude=longitude)
