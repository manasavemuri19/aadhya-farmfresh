"""Order queries specific to the delivery-agent flow.

Kept separate from OrderRepository: an agent's view of an order is a
different, much leaner shape (no payment details, no full timeline) than
the customer-facing OrderView, and the query filters here
(delivery_agent_id IS NULL / == me) don't belong on the general-purpose
repository other roles use.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Order as OrderRow
from app.domain.enums import OrderStatus
from app.domain.geo import bounding_box_km

# Delivered/cancelled/refunded orders are done, from an agent's point of
# view, whether or not they were the one who delivered it.
_ONGOING_STATUSES = {
    OrderStatus.CONFIRMED.value,
    OrderStatus.PACKED.value,
    OrderStatus.OUT_FOR_DELIVERY.value,
}

# AAD-REL-006: staff can reassign an order in any of these — the same set
# an agent can still be actively working. DELIVERED/CANCELLED/REFUNDED have
# nothing left to reassign.
_REASSIGNABLE_STATUSES = _ONGOING_STATUSES


def _to_dict(row: OrderRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "order_number": row.order_number,
        "status": row.status,
        "address": row.address,
        "notes": row.notes,
        "total_paise": row.total_paise,
        "item_count": sum(line.qty for line in row.lines),
        "created_at": row.created_at,
        "delivery_assigned_at": row.delivery_assigned_at,
    }


class DeliveryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_new_requests(
        self, *, limit: int = 100, near: tuple[float, float, float] | None = None
    ) -> list[dict[str, Any]]:
        """Paid, unassigned, ready for someone to accept. Oldest first — the
        order that's been waiting longest gets first crack at any agent who
        opens the app, not just whichever happens to be nearest.

        AAD-PERF-012: `near`, when given, is `(lat, lng, radius_km)` — the
        caller's own position and the radius it's currently trying. Without
        it (an agent with no location shared yet), this loads every
        candidate, same as always. With it, the WHERE clause adds a cheap
        bounding-box predicate (`domain.geo.bounding_box_km`) so a request
        for "what's near me at 2km" doesn't eager-load every pending order
        in the farm's whole service area just to discard most of them in
        Python. An order with no coordinates on its address at all always
        passes the filter regardless of the box — the caller still needs to
        see those (see `DeliveryService.list_requests`'s own docstring) —
        and the box itself is a deliberately generous rectangle, not the
        final answer: the caller re-checks the real distance with
        `haversine_km` on whatever this returns.
        """
        conditions = [
            OrderRow.status == OrderStatus.CONFIRMED.value,
            OrderRow.delivery_agent_id.is_(None),
        ]
        if near is not None:
            lat, lng, radius_km = near
            lat_min, lat_max, lng_min, lng_max = bounding_box_km(lat, lng, radius_km)
            lat_col = OrderRow.address["latitude"].as_float()
            lng_col = OrderRow.address["longitude"].as_float()
            conditions.append(
                or_(
                    lat_col.is_(None),
                    lng_col.is_(None),
                    and_(lat_col.between(lat_min, lat_max), lng_col.between(lng_min, lng_max)),
                )
            )
        stmt = (
            select(OrderRow)
            .options(selectinload(OrderRow.lines))
            .where(*conditions)
            .order_by(OrderRow.created_at)
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).scalars().unique().all()
        return [_to_dict(r) for r in rows]

    async def list_ongoing(self, agent_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        stmt = (
            select(OrderRow)
            .options(selectinload(OrderRow.lines))
            .where(
                OrderRow.delivery_agent_id == agent_id,
                OrderRow.status.in_(_ONGOING_STATUSES),
            )
            .order_by(OrderRow.delivery_assigned_at)
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).scalars().unique().all()
        return [_to_dict(r) for r in rows]

    async def accept(self, order_id: str, agent_id: str) -> dict[str, Any] | None:
        """Compare-and-swap on `delivery_agent_id IS NULL`.

        This — not comparing client-reported tap timestamps — is what
        actually decides "who got there first": exactly one concurrent
        UPDATE against the same row can win at the database level, full
        stop. For two requests that are genuinely simultaneous, which one
        the database happens to process first is effectively arbitrary,
        which is exactly the "pick either one" behaviour a tie should have
        — without trusting a client clock, which two different phones
        cannot be relied on to agree on down to the millisecond anyway.
        """
        result = await self.session.execute(
            update(OrderRow)
            .where(
                OrderRow.id == order_id,
                OrderRow.delivery_agent_id.is_(None),
                OrderRow.status == OrderStatus.CONFIRMED.value,
            )
            .values(delivery_agent_id=agent_id, delivery_assigned_at=datetime.now(UTC))
        )
        if result.rowcount != 1:
            return None

        row = (
            await self.session.execute(
                select(OrderRow)
                .options(selectinload(OrderRow.lines))
                .where(OrderRow.id == order_id)
            )
        ).scalars().first()
        return _to_dict(row) if row else None

    async def get_one(self, order_id: str, agent_id: str) -> dict[str, Any] | None:
        """This order, as this agent's own view of it — but only if it's
        actually assigned to them. Returning None otherwise isn't just a
        lookup detail: it's the authorization check `DeliveryService.
        update_status` relies on before letting an agent touch an order's
        status at all, so an agent can never advance the status of a job
        that isn't theirs, however they got the order id.
        """
        stmt = (
            select(OrderRow)
            .options(selectinload(OrderRow.lines))
            .where(OrderRow.id == order_id, OrderRow.delivery_agent_id == agent_id)
        )
        row = (await self.session.execute(stmt)).scalars().first()
        return _to_dict(row) if row else None

    async def count_ongoing(self, agent_id: str) -> int:
        """AAD-SEC-029: count only, no full-row loading — used by
        DeliveryService.accept to enforce a concurrent-order cap before
        accepting a new one."""
        result = await self.session.execute(
            select(func.count())
            .select_from(OrderRow)
            .where(
                OrderRow.delivery_agent_id == agent_id,
                OrderRow.status.in_(_ONGOING_STATUSES),
            )
        )
        return result.scalar_one()

    async def reassign(self, order_id: str, *, new_agent_id: str | None) -> bool:
        """AAD-REL-006: staff-only escape hatch — unlike `release` (agent-
        initiated, CONFIRMED-only), this works regardless of which agent
        currently holds the order, across any status still actively being
        worked. Passing new_agent_id=None sends it back to the unassigned
        pool (same effect as release, but usable past CONFIRMED)."""
        result = await self.session.execute(
            update(OrderRow)
            .where(
                OrderRow.id == order_id,
                OrderRow.status.in_(_REASSIGNABLE_STATUSES),
            )
            .values(
                delivery_agent_id=new_agent_id,
                delivery_assigned_at=datetime.now(UTC) if new_agent_id else None,
            )
        )
        return result.rowcount == 1

    async def release(self, order_id: str, agent_id: str) -> bool:
        """The mirror image of `accept`: only the agent currently holding the
        order can let it go, and only while it's still just-confirmed.

        Scoped to CONFIRMED on purpose — `list_new_requests` only ever
        surfaces confirmed-and-unassigned orders, so clearing
        `delivery_agent_id` on anything already packed or out for delivery
        would silently vanish it from every agent's view rather than
        actually sending it back to the pool. Once it's past that point,
        reassigning it is a farm-staff action, not a self-serve one.
        """
        result = await self.session.execute(
            update(OrderRow)
            .where(
                OrderRow.id == order_id,
                OrderRow.delivery_agent_id == agent_id,
                OrderRow.status == OrderStatus.CONFIRMED.value,
            )
            .values(delivery_agent_id=None, delivery_assigned_at=None)
        )
        return result.rowcount == 1
