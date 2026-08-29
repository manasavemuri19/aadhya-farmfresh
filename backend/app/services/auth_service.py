from __future__ import annotations

import logging

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

from app.core.config import settings
from app.core.errors import RateLimited, Unauthorized
from app.core.ids import new_otp_code
from app.core.security import decode_token, issue_access_token, issue_refresh_token
from app.repositories.otp import OtpRepository
from app.repositories.users import UserRepository
from app.schemas.auth import OtpRequestResponse, TokenPair, UserProfile

log = logging.getLogger(__name__)

_FAILURE_MESSAGE = "That code is not valid. Request a new one."
_GOOGLE_FAILURE_MESSAGE = "Could not sign in with Google. Try again."

# One HTTP transport, reused across verification calls rather than opened
# fresh each time — this is what the google-auth library expects to fetch
# Google's public signing keys with.
_google_transport = google_requests.Request()


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

    async def verify_google_and_login(self, id_token: str) -> tuple[TokenPair, UserProfile]:
        """Verify a Google-issued ID token and sign the person in, creating
        an account on first sign-in.

        Verification is delegated entirely to `google-auth`, which fetches
        and caches Google's public signing keys and checks the token's
        signature, expiry, and issuer — this function only adds the one
        check that library can't do for us: that the token was actually
        minted for *this* app, not some other app entirely (the classic
        token-substitution attack an audience check exists to prevent).
        """
        accepted_audiences = [
            aud for aud in (settings.google_web_client_id, settings.google_android_client_id) if aud
        ]
        if not accepted_audiences:
            log.error("google sign-in attempted with no client IDs configured")
            raise Unauthorized(_GOOGLE_FAILURE_MESSAGE)

        try:
            claims = google_id_token.verify_oauth2_token(id_token, _google_transport)
        except Exception:
            log.info("google id token failed verification")
            raise Unauthorized(_GOOGLE_FAILURE_MESSAGE) from None

        if claims.get("aud") not in accepted_audiences:
            log.warning("google id token had an unexpected audience")
            raise Unauthorized(_GOOGLE_FAILURE_MESSAGE)

        sub = claims.get("sub")
        if not sub:
            raise Unauthorized(_GOOGLE_FAILURE_MESSAGE)

        user = await self.users.get_or_create_by_google(
            google_sub=sub,
            email=claims.get("email", ""),
            name=claims.get("name", ""),
        )
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
            phone=user.get("phone"),
            email=user.get("email"),
            name=user.get("name", ""),
            role=user.get("role", "customer"),
            addresses=user.get("addresses", []),
        )
