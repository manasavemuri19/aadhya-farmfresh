"""The "still stuck?" fallback at the end of Help & Support."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import CurrentUser, get_support_service
from app.api.route import TransactionalRoute
from app.core.rate_limit import RateLimiter
from app.schemas.support import SupportTicketCreate, SupportTicketCreated
from app.services.support_service import SupportService

router = APIRouter(prefix="/support", tags=["support"], route_class=TransactionalRoute)

Support = Annotated[SupportService, Depends(get_support_service)]

# AAD-BIZ-005: this route sits behind `current_user`, so AAD-SEC-004's
# blanket 120/min-per-user limiter already applies — but that budget is
# shared across the entire API, not scoped to this endpoint, and it accepts
# up to 2,000 characters per call. A tighter, dedicated per-user window on
# top of the global one is what the finding actually asked for: five
# submissions an hour is generous for a genuine "still stuck" fallback and
# cheap to raise later if it turns out to be wrong.
_ticket_per_user_per_hour = RateLimiter(limit=5, seconds=3600)


async def _limit_ticket_submission(principal: CurrentUser) -> None:
    _ticket_per_user_per_hour.check(principal.user_id)


@router.post(
    "/tickets",
    response_model=SupportTicketCreated,
    status_code=201,
    dependencies=[Depends(_limit_ticket_submission)],
)
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
