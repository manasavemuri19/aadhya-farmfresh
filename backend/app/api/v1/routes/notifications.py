from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import CurrentUser, get_push_token_repo
from app.api.route import TransactionalRoute
from app.repositories.push_tokens import PushTokenRepository
from app.schemas.notifications import EXPO_PUSH_TOKEN_PATTERN, RegisterPushToken

router = APIRouter(prefix="/notifications", tags=["notifications"], route_class=TransactionalRoute)

PushTokens = Annotated[PushTokenRepository, Depends(get_push_token_repo)]


@router.post("/register-token", status_code=status.HTTP_204_NO_CONTENT)
async def register_token(
    body: RegisterPushToken, principal: CurrentUser, repo: PushTokens
) -> None:
    await repo.register(user_id=principal.user_id, token=body.token, platform=body.platform)


@router.delete("/token", status_code=status.HTTP_204_NO_CONTENT)
async def deregister_token(
    principal: CurrentUser,
    repo: PushTokens,
    token: Annotated[str, Query(min_length=8, max_length=200, pattern=EXPO_PUSH_TOKEN_PATTERN)],
) -> None:
    """AAD-SEC-032: nothing previously told the server a device had signed
    out — `signOut()` on the client only ever cleared its own local
    keychain. A shared, resold or returned handset kept receiving the
    previous user's order notifications until someone else signed in on it
    and happened to re-register the same token. Scoped to the calling
    user's own registration (`delete_for_user`) — a 204 either way, whether
    the token was theirs or was never registered at all, since neither
    case is something the caller needs to act on. The other half of this
    finding — pruning tokens no device has touched in 90 days, for the
    handsets that never call this at all — runs from the housekeeping
    sweep; see `PushTokenRepository.prune_stale`.
    """
    await repo.delete_for_user(user_id=principal.user_id, token=token)
