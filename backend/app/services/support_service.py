from __future__ import annotations

from app.repositories.support import SupportRepository
from app.schemas.support import SupportTicketCreated


class SupportService:
    def __init__(self, tickets: SupportRepository) -> None:
        self.tickets = tickets

    async def submit(
        self, *, user_id: str, message: str, context_node_id: str | None
    ) -> SupportTicketCreated:
        ticket = await self.tickets.create(
            user_id=user_id, message=message, context_node_id=context_node_id
        )
        return SupportTicketCreated(id=ticket.id, created_at=ticket.created_at)
