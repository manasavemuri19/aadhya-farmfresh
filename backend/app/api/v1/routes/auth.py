from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.deps import CurrentUser, get_auth_service, get_user_repo
from app.repositories.users import UserRepository
from app.schemas.auth import (
    Address,
    GoogleSignInRequest,
    RefreshRequest,
    TokenPair,
    UpdateProfile,
    UserProfile,
)
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])

AuthSvc = Annotated[AuthService, Depends(get_auth_service)]
Users = Annotated[UserRepository, Depends(get_user_repo)]


@router.post("/google")
async def google_sign_in(body: GoogleSignInRequest, svc: AuthSvc) -> dict:
    tokens, profile = await svc.verify_google_and_login(body.id_token)
    return {"tokens": tokens.model_dump(), "user": profile.model_dump()}


# Phone + OTP login has been retired in favour of Google sign-in as the only
# entry point — see AuthService for why: get_or_create_by_phone relied on
# phone being a unique column, and phone is now plain delivery contact info,
# not a login identity, so it can't safely stay unique. The OTP repository,
# its idempotent-replay logic, and its tests are left in place (not deleted)
# in case phone login is ever wanted again — only the routes are gone.


@router.post("/refresh", response_model=TokenPair)
async def refresh(body: RefreshRequest, svc: AuthSvc) -> TokenPair:
    return await svc.refresh(body.refresh_token)


@router.get("/me", response_model=UserProfile)
async def me(principal: CurrentUser, svc: AuthSvc) -> UserProfile:
    return await svc.get_profile(principal.user_id)


@router.patch("/me", response_model=UserProfile)
async def update_me(
    body: UpdateProfile, principal: CurrentUser, users: Users, svc: AuthSvc
) -> UserProfile:
    """Name, phone, and address all in one call — deliberately, not three.

    This used to be three separate round trips from the app (name, then
    phone, then address), fired back-to-back. On a shaky connection that is
    three separate chances to fail, and if the third one failed the first
    two had already committed — leaving an account with a saved name and
    phone but no address, invisible until the next screen tried to use it.
    One request, one transaction: either the whole profile update lands, or
    none of it does.
    """
    changes = body.model_dump(exclude={"address"}, exclude_none=True)
    if changes:
        await users.update_profile(principal.user_id, changes)
    if body.address is not None:
        await users.upsert_address(principal.user_id, body.address.model_dump(mode="json"))
    return await svc.get_profile(principal.user_id)


@router.put("/me/addresses", status_code=status.HTTP_204_NO_CONTENT)
async def save_address(body: Address, principal: CurrentUser, users: Users) -> None:
    await users.upsert_address(principal.user_id, body.model_dump(mode="json"))
