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

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Order as OrderRow
from app.db.models import OrderLine
from app.domain.enums import OrderStatus

# Delivered/cancelled/refunded orders are done, from an agent's point of
# view, whether or not they were the one who delivered it.
_ONGOING_STATUSES = {
    OrderStatus.CONFIRMED.value,
    OrderStatus.PACKED.value,
    OrderStatus.OUT_FOR_DELIVERY.value,
}


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

    async def list_new_requests(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Paid, unassigned, ready for someone to accept. Oldest first — the
        order that's been waiting longest gets first crack at any agent who
        opens the app, not just whichever happens to be nearest."""
        stmt = (
            select(OrderRow)
            .options(selectinload(OrderRow.lines))
            .where(
                OrderRow.status == OrderStatus.CONFIRMED.value,
                OrderRow.delivery_agent_id.is_(None),
            )
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
