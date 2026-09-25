from __future__ import annotations

import logging
from datetime import datetime

from app.core.outbox import defer_until_commit
from app.domain.enums import SupportTicketStatus
from app.repositories.support import SupportRepository
from app.repositories.users import UserRepository
from app.schemas.common import Page
from app.schemas.support import SupportTicketCreated, SupportTicketView
from app.services.push_service import PushService

log = logging.getLogger(__name__)


class SupportService:
    def __init__(
        self,
        tickets: SupportRepository,
        *,
        users: UserRepository | None = None,
        push: PushService | None = None,
    ) -> None:
        # AAD-BIZ-005: users/push are optional, same shape as OrderService's
        # own notification dependencies — None on any caller that doesn't
        # go through the HTTP dependency chain (there are none today, but
        # this keeps the constructor usable standalone, e.g. in tests).
        self.tickets = tickets
        self.users = users
        self.push = push

    async def submit(
        self, *, user_id: str, message: str, context_node_id: str | None
    ) -> SupportTicketCreated:
        ticket = await self.tickets.create(
            user_id=user_id, message=message, context_node_id=context_node_id
        )
        # AAD-BIZ-005: until now this insert was the only trace a ticket
        # ever left — nothing read the table, nothing logged its arrival.
        # `warning` (not `info`) is deliberate: per AAD-OPS-022's own
        # framing, this is one of the few channels most of this audit's
        # production consequences would actually surface through, so it
        # needs to be the kind of line someone watching logs would notice,
        # not routine traffic.
        log.warning(
            "support ticket submitted",
            extra={"ticket_id": ticket.id, "user_id": user_id, "context_node_id": context_node_id},
        )
        await self._notify_staff(ticket.id)
        return SupportTicketCreated(id=ticket.id, created_at=ticket.created_at)

    async def _notify_staff(self, ticket_id: str) -> None:
        """Best-effort push to every staff/admin account, deferred until the
        commit that created this ticket actually lands — same reasoning and
        same mechanism as OrderService's `_notify_agents_new_order`
        (AAD-REL-004): a push for a ticket that got rolled back would tell
        staff about a submission that, from the database's point of view,
        never happened."""
        if not self.push or not self.users:
            return
        staff_ids = await self.users.list_staff_ids()
        if not staff_ids:
            return
        push = self.push
        await defer_until_commit(
            lambda: push.notify_users(
                staff_ids,
                title="New support ticket",
                body="A customer submitted a support ticket.",
                data={"ticket_id": ticket_id},
            )
        )

    async def list_for_staff(
        self,
        *,
        status: SupportTicketStatus | None = None,
        limit: int = 50,
        after: datetime | None = None,
    ) -> Page[SupportTicketView]:
        """AAD-BIZ-005: the admin read path. Defaults to no status filter —
        an unfiltered "oldest first" queue naturally surfaces old open
        tickets before the closed ones that come after them chronologically
        only once someone works through the backlog; passing `status=open`
        gives the equivalent of a work queue."""
        rows = await self.tickets.list(status=status, limit=limit, after=after)
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = rows[-1].created_at.isoformat() if has_more and rows else None
        items = [
            SupportTicketView(
                id=t.id,
                user_id=t.user_id,
                message=t.message,
                context_node_id=t.context_node_id,
                status=t.status,
                created_at=t.created_at,
            )
            for t in rows
        ]
        return Page(items=items, next_cursor=next_cursor, has_more=has_more)

    async def close(self, ticket_id: str) -> bool:
        return await self.tickets.close(ticket_id)
