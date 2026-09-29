"""AAD-BIZ-004: COD cash reconciliation — the admin-facing half.

The customer/agent-facing half (recording a COD collection) lives in
`OrderService.verify_delivery_code`, since it has to happen atomically with
delivery verification, not as a separate call an agent could skip or
double-submit. This service is only the settlement action: an admin
records what an agent physically handed back, and this compares it against
what that agent's unclaimed collections say they should have.
"""

from __future__ import annotations

import logging

from app.core.errors import Conflict, ValidationError
from app.core.ids import new_id
from app.domain.enums import Role, SettlementStatus
from app.repositories.cash import CashRepository
from app.repositories.users import UserRepository
from app.schemas.cash import AgentCashOrder, AgentPendingCash, MyCashView, SettlementView

log = logging.getLogger(__name__)


class CashService:
    def __init__(self, cash: CashRepository, users: UserRepository) -> None:
        self.cash = cash
        self.users = users

    async def settle_agent(
        self, *, agent_id: str, actual_amount_paise: int, reason: str, actor_id: str
    ) -> SettlementView:
        """Lock this agent's unclaimed COD collections, total them as
        `expected`, and compare against `actual_amount_paise` — what an
        admin is recording as physically received from that agent.

        Ordering matters here, and it's the whole race-safety and
        referential-integrity story:

        1. `pending_for_agent` takes the row lock (`FOR UPDATE`) — a second
           settlement call for the same agent blocks on this until the
           first commits, then finds nothing left pending (see that
           method's own docstring). This is also `AAD-BIZ-004`'s
           duplicate-settlement guard: nothing pending raises `Conflict`.
        2. The settlement row is inserted *before* the collections are
           claimed into it, so `CodCollection.settlement_id`'s foreign key
           always points at a row that already exists.
        3. Only then are the locked collections claimed — one-way, as
           `CashRepository.claim_for_settlement` documents.
        """
        agent = await self.users.get_by_id(agent_id)
        if agent is None or agent.get("role") != Role.DELIVERY_AGENT.value:
            raise ValidationError("agent_id must be an existing delivery agent.")
        if actual_amount_paise < 0:
            raise ValidationError("actual_amount_paise cannot be negative.")

        pending = await self.cash.pending_for_agent(agent_id)
        if not pending:
            raise Conflict("This agent has no pending COD cash to settle.")

        expected = sum(row["amount_paise"] for row in pending)
        discrepancy = actual_amount_paise - expected

        # AAD-BIZ-004: never silently marked settled. An exact match is the
        # only way to `SETTLED`; anything else is `DISCREPANCY` and needs a
        # reason on record, not just a number that doesn't add up.
        if discrepancy != 0 and not reason.strip():
            raise ValidationError(
                "A reason is required when the amount received doesn't match "
                f"the expected ₹{expected / 100:.2f}."
            )
        status = SettlementStatus.SETTLED if discrepancy == 0 else SettlementStatus.DISCREPANCY

        settlement = await self.cash.insert_settlement(
            settlement_id=new_id("stl"),
            agent_id=agent_id,
            expected_amount_paise=expected,
            actual_amount_paise=actual_amount_paise,
            discrepancy_paise=discrepancy,
            status=status.value,
            reason=reason.strip(),
            recorded_by=actor_id,
        )
        await self.cash.claim_for_settlement(
            [row["id"] for row in pending], settlement["id"]
        )
        log.info(
            "cod_cash_settled",
            extra={
                "settlement_id": settlement["id"],
                "agent_id": agent_id,
                "expected_amount_paise": expected,
                "actual_amount_paise": actual_amount_paise,
                "discrepancy_paise": discrepancy,
                "status": status.value,
                "orders_settled": len(pending),
                "recorded_by": actor_id,
            },
        )
        return SettlementView(
            id=settlement["id"],
            agent_id=agent_id,
            expected_amount_paise=expected,
            actual_amount_paise=actual_amount_paise,
            discrepancy_paise=discrepancy,
            status=status,
            reason=settlement["reason"],
            orders_settled=len(pending),
            created_at=settlement["created_at"],
        )

    # ---------- read side: owner's Cash tab, agent's own line ----------

    async def pending_by_agent(self) -> list[AgentPendingCash]:
        """One card per agent holding unsettled COD cash, largest first."""
        groups = await self.cash.pending_by_agent()
        names = await self.users.names_by_id([g["agent_id"] for g in groups])
        cards = [
            AgentPendingCash(
                agent_id=g["agent_id"],
                agent_name=(names.get(g["agent_id"]) or {}).get("name") or None,
                agent_phone=(names.get(g["agent_id"]) or {}).get("phone"),
                pending_amount_paise=g["pending_amount_paise"],
                orders=[AgentCashOrder(**o) for o in g["orders"]],
            )
            for g in groups
        ]
        cards.sort(key=lambda c: c.pending_amount_paise, reverse=True)
        return cards

    async def my_cash(self, agent_id: str) -> MyCashView:
        amount, count = await self.cash.pending_total_for_agent(agent_id)
        return MyCashView(pending_amount_paise=amount, orders_count=count)

    async def recent_settlements(self, *, limit: int = 30) -> list[SettlementView]:
        rows = await self.cash.list_settlements(limit=limit)
        names = await self.users.names_by_id(list({r["agent_id"] for r in rows}))
        return [
            SettlementView(
                id=r["id"],
                agent_id=r["agent_id"],
                expected_amount_paise=r["expected_amount_paise"],
                actual_amount_paise=r["actual_amount_paise"],
                discrepancy_paise=r["discrepancy_paise"],
                status=r["status"],
                reason=r["reason"],
                orders_settled=r["orders_settled"],
                created_at=r["created_at"],
                agent_name=(names.get(r["agent_id"]) or {}).get("name") or None,
            )
            for r in rows
        ]
