"""Support tickets — an insert-list-close mailbox; no assignment, no reply
thread yet (see SupportTicket's own docstring, and AAD-BIZ-005)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import new_support_ticket_id
from app.db.models import SupportTicket
from app.domain.enums import SupportTicketStatus


class SupportRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self, *, user_id: str, message: str, context_node_id: str | None
    ) -> SupportTicket:
        ticket = SupportTicket(
            id=new_support_ticket_id(),
            user_id=user_id,
            message=message,
            context_node_id=context_node_id,
            status=SupportTicketStatus.OPEN.value,
        )
        self.session.add(ticket)
        await self.session.flush()
        return ticket

    async def list(
        self,
        *,
        status: SupportTicketStatus | None = None,
        limit: int = 50,
        after: datetime | None = None,
    ) -> list[SupportTicket]:
        """AAD-BIZ-005: oldest first — a work queue, not a feed, same as the
        staff order queue (`OrderRepository.list_by_status`). `limit + 1` is
        that method's one-extra-row trick, so the caller can tell whether
        there's another page without a separate COUNT."""
        stmt = select(SupportTicket).order_by(SupportTicket.created_at).limit(limit + 1)
        if status is not None:
            stmt = stmt.where(SupportTicket.status == status.value)
        if after:
            stmt = stmt.where(SupportTicket.created_at > after)
        return list((await self.session.execute(stmt)).scalars().all())

    async def close(self, ticket_id: str) -> bool:
        """Marks a ticket handled. The WHERE also requires it currently be
        `open`, so this is a CAS, not a blind write — a double-close (two
        staff tabs, a retried request) reports `False` on the second call
        instead of silently succeeding twice."""
        result = await self.session.execute(
            sa_update(SupportTicket)
            .where(
                SupportTicket.id == ticket_id,
                SupportTicket.status == SupportTicketStatus.OPEN.value,
            )
            .values(status=SupportTicketStatus.CLOSED.value)
        )
        return result.rowcount > 0
