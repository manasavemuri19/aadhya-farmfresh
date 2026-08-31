"""Support tickets — a plain insert-and-read mailbox, no update path."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import new_support_ticket_id
from app.db.models import SupportTicket


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
        )
        self.session.add(ticket)
        await self.session.flush()
        return ticket
