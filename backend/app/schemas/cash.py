"""AAD-BIZ-004: COD cash-settlement wire models."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.core.money import MAX_PAISE
from app.domain.enums import SettlementStatus
from app.schemas.common import Schema


class RecordSettlementRequest(Schema):
    agent_id: str
    # AAD-QUAL-014-style bound: reuses the same sanity ceiling every other
    # money field in this app is capped at, not a business rule of its own.
    actual_amount_received_paise: int = Field(ge=0, le=MAX_PAISE)
    # Required only when the amount doesn't match what's expected —
    # CashService.settle_agent enforces that, not this schema, since
    # "expected" isn't known until the agent's pending cash is looked up.
    reason: str = Field(default="", max_length=300)


class SettlementView(Schema):
    id: str
    agent_id: str
    expected_amount_paise: int
    actual_amount_paise: int
    discrepancy_paise: int
    status: SettlementStatus
    reason: str
    orders_settled: int
    created_at: datetime
    # Cash tab history label — filled in by the list endpoint, not by
    # settle_agent's own response (the caller already knows who it paid).
    agent_name: str | None = None


class AgentCashOrder(Schema):
    order_id: str
    order_number: str
    amount_paise: int
    collected_at: datetime


class AgentPendingCash(Schema):
    """One delivery agent's unsettled COD cash — a card on the Cash tab."""

    agent_id: str
    agent_name: str | None
    agent_phone: str | None
    pending_amount_paise: int
    orders: list[AgentCashOrder]


class MyCashView(Schema):
    """The delivery agent's own "cash to hand over" line."""

    pending_amount_paise: int
    orders_count: int
