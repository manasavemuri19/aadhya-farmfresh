from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.deps import CurrentUser, get_auth_service, get_order_repo, get_user_repo
from app.api.route import TransactionalRoute
from app.core.errors import NotFound
from app.core.rate_limit import IpRateLimiter
from app.repositories.orders import OrderRepository
from app.repositories.users import UserRepository
from app.schemas.auth import (
    Address,
    GoogleSignInRequest,
    GoogleSignInResponse,
    RefreshRequest,
    TokenPair,
    UpdateProfile,
    UserProfile,
)
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"], route_class=TransactionalRoute)

AuthSvc = Annotated[AuthService, Depends(get_auth_service)]
Users = Annotated[UserRepository, Depends(get_user_repo)]
Orders = Annotated[OrderRepository, Depends(get_order_repo)]

# AAD-SEC-004: both unauthenticated and each triggers a database write (and,
# for /google, a blocking-shaped outbound call before AAD-SEC-003's cache is
# warm) — the fix's own suggested starting limits, keyed on the trusted
# client IP AAD-SEC-005 established. Two independent windows on /google
# (a tight per-minute one and a looser per-hour one) catch both a tight burst
# and a slow, sustained drip the per-minute window alone wouldn't.
_google_per_minute = IpRateLimiter(limit=10, seconds=60)
_google_per_hour = IpRateLimiter(limit=30, seconds=3600)
_refresh_per_minute = IpRateLimiter(limit=20, seconds=60)


@router.post(
    "/google",
    response_model=GoogleSignInResponse,
    dependencies=[Depends(_google_per_minute), Depends(_google_per_hour)],
)
async def google_sign_in(body: GoogleSignInRequest, svc: AuthSvc) -> GoogleSignInResponse:
    tokens, profile = await svc.verify_google_and_login(body.id_token)
    return GoogleSignInResponse(tokens=tokens, user=profile)


# Phone + OTP login has been retired in favour of Google sign-in as the only
# entry point — see AuthService for why: get_or_create_by_phone relied on
# phone being a unique column, and phone is now plain delivery contact info,
# not a login identity, so it can't safely stay unique. AAD-QUAL-001: the OTP
# repository, service methods, settings and tests that used to sit here
# "in case phone login is ever wanted again" are deleted, not just the
# routes — see the fix write-up for why. Recoverable from git history
# (this commit) if phone login is ever deliberately rebuilt.


@router.post("/refresh", response_model=TokenPair, dependencies=[Depends(_refresh_per_minute)])
async def refresh(body: RefreshRequest, svc: AuthSvc) -> TokenPair:
    return await svc.refresh(body.refresh_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(body: RefreshRequest, svc: AuthSvc) -> None:
    """AAD-SEC-002: there used to be no way to end a session server-side at
    all — `signOut()` on the client only ever cleared the local keychain.
    Revokes the one session this refresh token belongs to; deliberately
    unauthenticated (no CurrentUser) because the whole point is to let a
    client whose access token has already expired still sign out cleanly.
    """
    await svc.logout(body.refresh_token)


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(principal: CurrentUser, svc: AuthSvc) -> None:
    """The "sign out of everywhere" button — every refresh token this user
    has is revoked immediately. Requires a currently-valid access token,
    unlike `/logout`: this is a decision to make about the account, not a
    routine sign-out, so it stays behind normal authentication."""
    await svc.logout_all(principal.user_id)


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


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
async def delete_me(
    principal: CurrentUser, users: Users, orders: Orders, svc: AuthSvc
) -> None:
    """AAD-API-002 / AAD-DATA-006: the DPDP Act 2023 right-to-erasure
    endpoint that never existed — until now, no account with an order could
    be deleted at all (`orders.user_id` is `ON DELETE RESTRICT`, correctly,
    to protect tax and accounting records), which meant this obligation was
    not just unimplemented but impossible to satisfy through the schema as
    it stood.

    This stays impossible to satisfy by *deleting* anything — the fix here
    is to separate erasure from deletion instead: order rows are untouched,
    but every piece of personal data reachable from this account is. That's
    three things, in order: every refresh token this user holds is revoked
    right away (`logout_all`'s own job, reused rather than duplicated, so a
    stolen or still-open session dies with the account instead of
    outliving it up to its normal 30-day ceiling); the user row itself is
    anonymised and every saved address deleted outright
    (`UserRepository.erase`); and the delivery address snapshotted onto
    every one of this user's past orders is replaced with a placeholder
    (`OrderRepository.anonymize_addresses_for_user`) — the one piece of
    personal data that lives outside the `users`/`addresses` tables
    entirely. A stale access token already in flight still works for up to
    30 minutes, the same accepted window AAD-SEC-002 already documented for
    a suspended account — this doesn't newly introduce that gap.
    """
    await svc.logout_all(principal.user_id)
    await users.erase(principal.user_id)
    await orders.anonymize_addresses_for_user(principal.user_id)


@router.put("/me/addresses", status_code=status.HTTP_204_NO_CONTENT)
async def save_address(body: Address, principal: CurrentUser, users: Users) -> None:
    """Create a new saved address (under `body.label`), or replace an
    existing one's fields *without* changing its label — for a genuine
    rename, use `PATCH .../addresses/{label}` below instead, which is the
    one that actually relabels a row rather than creating a second one
    beside it."""
    await users.upsert_address(principal.user_id, body.model_dump(mode="json"))


@router.patch("/me/addresses/{label}", status_code=status.HTTP_204_NO_CONTENT)
async def rename_address(
    label: str, body: Address, principal: CurrentUser, users: Users
) -> None:
    """AAD-MOB-022 (multi-address support): relabel the address currently
    saved as `label` to `body.label` (and update its other fields to
    `body`'s at the same time) — a real rename, not a second address left
    beside the first. `label` in the path is the address's *current* name;
    `body.label` is what it's being renamed to (it may also just equal
    `label`, which makes this behave exactly like the plain PUT above —
    updating fields with no rename)."""
    await users.upsert_address(
        principal.user_id, body.model_dump(mode="json"), previous_label=label
    )


@router.delete("/me/addresses/{label}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_address(label: str, principal: CurrentUser, users: Users) -> None:
    deleted = await users.delete_address(principal.user_id, label)
    if not deleted:
        raise NotFound(f'No saved address labelled "{label}".')
