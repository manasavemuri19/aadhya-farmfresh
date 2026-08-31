"""The "still stuck?" fallback at the end of Help & Support."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import CurrentUser, get_support_service
from app.schemas.support import SupportTicketCreate, SupportTicketCreated
from app.services.support_service import SupportService

router = APIRouter(prefix="/support", tags=["support"])

Support = Annotated[SupportService, Depends(get_support_service)]


@router.post("/tickets", response_model=SupportTicketCreated, status_code=201)
async def create_support_ticket(
    body: SupportTicketCreate,
    principal: CurrentUser,
    svc: Support,
) -> SupportTicketCreated:
    return await svc.submit(
        user_id=principal.user_id,
        message=body.message,
        context_node_id=body.context_node_id,
    )
