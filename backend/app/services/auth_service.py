from __future__ import annotations

import logging

from app.core.config import settings
from app.core.errors import RateLimited, Unauthorized
from app.core.ids import new_otp_code
from app.core.security import decode_token, issue_access_token, issue_refresh_token
from app.repositories.otp import OtpRepository
from app.repositories.users import UserRepository
from app.schemas.auth import OtpRequestResponse, TokenPair, UserProfile

log = logging.getLogger(__name__)

_FAILURE_MESSAGE = "That code is not valid. Request a new one."


class AuthService:
    def __init__(self, users: UserRepository, otps: OtpRepository) -> None:
        self.users = users
        self.otps = otps

    async def request_otp(self, phone: str) -> OtpRequestResponse:
        wait = await self.otps.seconds_until_resend_allowed(phone)
        if wait > 0:
            raise RateLimited(
                f"Wait {wait} seconds before requesting another code.",
                details={"retry_after_seconds": wait},
            )

        code = new_otp_code()
        await self.otps.create(phone, code)
        await self._deliver(phone, code)

        return OtpRequestResponse(
            sent=True,
            expires_in_seconds=settings.otp_ttl_seconds,
            resend_after_seconds=settings.otp_resend_cooldown_seconds,
            debug_code=code if settings.otp_debug_echo else None,
        )

    async def _deliver(self, phone: str, code: str) -> None:
        """Hand the code to an SMS gateway.

        Left as a seam on purpose: plug in MSG91, Gupshup or Twilio here. The
        code is never logged, in any environment — a log aggregator is not a
        place where working credentials should end up.
        """
        if settings.otp_debug_echo:
            log.info("otp issued (debug echo on)", extra={"phone_suffix": phone[-4:]})
            return
        log.info("otp dispatch requested", extra={"phone_suffix": phone[-4:]})
        # TODO(sms): integrate the chosen provider before production launch.

    async def verify_otp(self, phone: str, code: str) -> tuple[TokenPair, UserProfile]:
        ok, reason = await self.otps.verify_and_consume(phone, code)
        if not ok:
            # One message for every failure mode: a distinct "expired" vs
            # "wrong code" reply tells an attacker which phones are mid-login.
            log.info("otp verification failed", extra={"reason": reason})
            raise Unauthorized(_FAILURE_MESSAGE)

        user = await self.users.get_or_create_by_phone(phone)
        return self._issue_tokens(user), self._to_profile(user)

    async def refresh(self, refresh_token: str) -> TokenPair:
        payload = decode_token(refresh_token, expected_type="refresh")
        user = await self.users.get_by_id(payload["sub"])
        if not user:
            raise Unauthorized("Sign in again.")
        return self._issue_tokens(user)

    async def get_profile(self, user_id: str) -> UserProfile:
        user = await self.users.get_by_id(user_id)
        if not user:
            raise Unauthorized("Sign in again.")
        return self._to_profile(user)

    def _issue_tokens(self, user: dict) -> TokenPair:
        return TokenPair(
            access_token=issue_access_token(user["id"], role=user.get("role", "customer")),
            refresh_token=issue_refresh_token(user["id"]),
            expires_in=settings.access_token_ttl_min * 60,
        )

    @staticmethod
    def _to_profile(user: dict) -> UserProfile:
        return UserProfile(
            id=user["id"],
            phone=user["phone"],
            name=user.get("name", ""),
            role=user.get("role", "customer"),
            addresses=user.get("addresses", []),
        )
