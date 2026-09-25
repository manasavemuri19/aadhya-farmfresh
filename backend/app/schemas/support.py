"""Wire models for Help & Support's "still stuck?" fallback."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.schemas.common import Schema


class SupportTicketCreate(Schema):
    message: str = Field(min_length=1, max_length=2000)
    context_node_id: str | None = Field(default=None, max_length=64)


class SupportTicketCreated(Schema):
    id: str
    created_at: datetime


class SupportTicketView(Schema):
    """AAD-BIZ-005: the admin-facing read of a ticket — `GET
    /admin/support/tickets`. Includes `user_id` (staff need to know who
    wrote in) but nothing beyond what `create` already stored; this is
    still the same disclosed-minimal mailbox, just now readable."""

    id: str
    user_id: str
    message: str
    context_node_id: str | None
    status: str
    created_at: datetime
