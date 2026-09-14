from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.deps import CurrentUser, get_push_token_repo
from app.api.route import TransactionalRoute
from app.repositories.push_tokens import PushTokenRepository
from app.schemas.notifications import RegisterPushToken

router = APIRouter(prefix="/notifications", tags=["notifications"], route_class=TransactionalRoute)

PushTokens = Annotated[PushTokenRepository, Depends(get_push_token_repo)]


@router.post("/register-token", status_code=status.HTTP_204_NO_CONTENT)
async def register_token(
    body: RegisterPushToken, principal: CurrentUser, repo: PushTokens
) -> None:
    await repo.register(user_id=principal.user_id, token=body.token, platform=body.platform)
