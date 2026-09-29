"""AAD-BIZ-004: COD cash tracking and settlement.

Two tables, two write paths, and they're deliberately never conflated:
`record_collection` is written once, automatically, by `OrderService.
verify_delivery_code` the moment a COD order's delivery code is verified —
nothing an agent enters. `settle_agent` is an admin action, initiated only
when cash has actually changed hands in person; nothing here calls a
gateway or moves real money, it only records what an admin says was
received and compares it to what's expected.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CashSettlement, CodCollection
from app.db.models import Order as OrderRow


def _collection_to_dict(row: CodCollection) -> dict[str, Any]:
    return {
        "id": row.id,
        "order_id": row.order_id,
        "agent_id": row.agent_id,
        "amount_paise": row.amount_paise,
        "collected_at": row.collected_at,
        "settlement_id": row.settlement_id,
    }


def _settlement_to_dict(row: CashSettlement) -> dict[str, Any]:
    return {
        "id": row.id,
        "agent_id": row.agent_id,
        "expected_amount_paise": row.expected_amount_paise,
        "actual_amount_paise": row.actual_amount_paise,
        "discrepancy_paise": row.discrepancy_paise,
        "status": row.status,
        "reason": row.reason,
        "recorded_by": row.recorded_by,
        "created_at": row.created_at,
    }


class CashRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record_collection(
        self, *, collection_id: str, order_id: str, agent_id: str, amount_paise: int
    ) -> dict[str, Any] | None:
        """Write one COD collection row. `order_id` is UNIQUE at the
        database level, so this is `INSERT ... ON CONFLICT DO NOTHING`
        rather than a plain insert: if a collection for this order somehow
        already exists (it shouldn't — the delivery OTP that gates this
        call is itself single-use — this is defence in depth, not the
        primary guard), the conflict is absorbed here rather than raised,
        and `None` tells the caller nothing new was written.
        """
        stmt = (
            insert(CodCollection)
            .values(
                id=collection_id,
                order_id=order_id,
                agent_id=agent_id,
                amount_paise=amount_paise,
            )
            .on_conflict_do_nothing(index_elements=["order_id"])
            .returning(CodCollection)
        )
        row = (await self.session.execute(stmt)).scalars().first()
        return _collection_to_dict(row) if row else None

    async def pending_for_agent(self, agent_id: str) -> list[dict[str, Any]]:
        """This agent's unclaimed collections, row-locked (`FOR UPDATE`).

        The lock is the whole race-safety story for settlement: a second
        concurrent call for the same agent blocks here until the first
        transaction commits, at which point every row this would have
        returned already has `settlement_id` set and is filtered out —
        so it correctly sees nothing pending rather than double-claiming.
        Ordered oldest-first purely so a settlement's total is
        reproducible/debuggable, not for any correctness reason.
        """
        stmt = (
            select(CodCollection)
            .where(CodCollection.agent_id == agent_id, CodCollection.settlement_id.is_(None))
            .order_by(CodCollection.collected_at)
            .with_for_update()
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return [_collection_to_dict(r) for r in rows]

    async def claim_for_settlement(self, collection_ids: list[str], settlement_id: str) -> None:
        """One-way: a claimed collection's `settlement_id` is never cleared
        back to NULL by anything in this codebase, whatever the
        settlement's own status turns out to be — see `CashSettlement`'s
        docstring for why a discrepancy is a new settlement against
        whatever's still pending, not a reopening of this one."""
        if not collection_ids:
            return
        await self.session.execute(
            update(CodCollection)
            .where(CodCollection.id.in_(collection_ids))
            .values(settlement_id=settlement_id)
        )

    async def insert_settlement(
        self,
        *,
        settlement_id: str,
        agent_id: str,
        expected_amount_paise: int,
        actual_amount_paise: int,
        discrepancy_paise: int,
        status: str,
        reason: str,
        recorded_by: str,
    ) -> dict[str, Any]:
        row = CashSettlement(
            id=settlement_id,
            agent_id=agent_id,
            expected_amount_paise=expected_amount_paise,
            actual_amount_paise=actual_amount_paise,
            discrepancy_paise=discrepancy_paise,
            status=status,
            reason=reason,
            recorded_by=recorded_by,
        )
        self.session.add(row)
        await self.session.flush()
        return _settlement_to_dict(row)

    async def get_settlement(self, settlement_id: str) -> dict[str, Any] | None:
        row = await self.session.get(CashSettlement, settlement_id)
        return _settlement_to_dict(row) if row else None

    # ---------- AAD-BIZ-004: read side for the owner's Cash tab and the ----------
    # ---------- agent's own "cash to hand over" line.                   ----------
    #
    # Deliberately unlocked reads — these only display. The settlement write
    # path still goes through `pending_for_agent` (FOR UPDATE), so a figure
    # shown here that goes stale a second later can't cause a wrong
    # settlement: `settle_agent` re-totals under the lock and records what
    # it actually finds.

    async def pending_by_agent(self) -> list[dict[str, Any]]:
        """Every unsettled COD collection, grouped by agent, with the order
        numbers — oldest collection first within each agent."""
        stmt = (
            select(
                CodCollection.agent_id,
                CodCollection.order_id,
                CodCollection.amount_paise,
                CodCollection.collected_at,
                OrderRow.order_number,
            )
            .join(OrderRow, OrderRow.id == CodCollection.order_id)
            .where(CodCollection.settlement_id.is_(None))
            .order_by(CodCollection.agent_id, CodCollection.collected_at)
        )
        rows = (await self.session.execute(stmt)).all()
        grouped: dict[str, dict[str, Any]] = {}
        for r in rows:
            g = grouped.setdefault(
                r.agent_id, {"agent_id": r.agent_id, "pending_amount_paise": 0, "orders": []}
            )
            g["pending_amount_paise"] += r.amount_paise
            g["orders"].append(
                {
                    "order_id": r.order_id,
                    "order_number": r.order_number,
                    "amount_paise": r.amount_paise,
                    "collected_at": r.collected_at,
                }
            )
        return list(grouped.values())

    async def pending_total_for_agent(self, agent_id: str) -> tuple[int, int]:
        """(amount_paise, order_count) this agent currently holds."""
        stmt = select(
            func.coalesce(func.sum(CodCollection.amount_paise), 0), func.count(CodCollection.id)
        ).where(CodCollection.agent_id == agent_id, CodCollection.settlement_id.is_(None))
        amount, count = (await self.session.execute(stmt)).one()
        return int(amount), int(count)

    async def list_settlements(self, *, limit: int = 30) -> list[dict[str, Any]]:
        """Most recent first, with how many orders each one covered."""
        count_sub = (
            select(CodCollection.settlement_id, func.count(CodCollection.id).label("n"))
            .group_by(CodCollection.settlement_id)
            .subquery()
        )
        stmt = (
            select(CashSettlement, func.coalesce(count_sub.c.n, 0))
            .outerjoin(count_sub, count_sub.c.settlement_id == CashSettlement.id)
            .order_by(CashSettlement.created_at.desc())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).all()
        out = []
        for settlement, n in rows:
            d = _settlement_to_dict(settlement)
            d["orders_settled"] = int(n)
            out.append(d)
        return out
