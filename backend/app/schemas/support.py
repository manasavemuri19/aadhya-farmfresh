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
