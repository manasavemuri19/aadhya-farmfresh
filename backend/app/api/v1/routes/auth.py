from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.deps import CurrentUser, get_auth_service, get_user_repo
from app.repositories.users import UserRepository
from app.schemas.auth import (
    Address,
    OtpRequest,
    OtpRequestResponse,
    OtpVerify,
    RefreshRequest,
    TokenPair,
    UpdateProfile,
    UserProfile,
)
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])

AuthSvc = Annotated[AuthService, Depends(get_auth_service)]
Users = Annotated[UserRepository, Depends(get_user_repo)]


@router.post("/otp/request", response_model=OtpRequestResponse)
async def request_otp(body: OtpRequest, svc: AuthSvc) -> OtpRequestResponse:
    return await svc.request_otp(body.phone)


@router.post("/otp/verify")
async def verify_otp(body: OtpVerify, svc: AuthSvc) -> dict:
    tokens, profile = await svc.verify_otp(body.phone, body.code)
    return {"tokens": tokens.model_dump(), "user": profile.model_dump()}


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
    await users.update_profile(principal.user_id, body.model_dump(exclude_none=True))
    return await svc.get_profile(principal.user_id)


@router.put("/me/addresses", status_code=status.HTTP_204_NO_CONTENT)
async def save_address(body: Address, principal: CurrentUser, users: Users) -> None:
    await users.upsert_address(principal.user_id, body.model_dump(mode="json"))
