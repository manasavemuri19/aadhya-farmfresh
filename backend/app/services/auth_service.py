from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from app.core.config import settings
from app.core.errors import Unauthorized, UpstreamError
from app.core.ids import new_refresh_token_id
from app.core.security import decode_token, issue_access_token, issue_refresh_token
from app.domain.enums import UserStatus
from app.repositories.refresh_tokens import RefreshTokenRepository, hash_token
from app.repositories.users import UserRepository
from app.schemas.auth import TokenPair, UserProfile

log = logging.getLogger(__name__)

_GOOGLE_FAILURE_MESSAGE = "Could not sign in with Google. Try again."
_GOOGLE_UPSTREAM_MESSAGE = "Could not reach Google to verify your sign-in. Try again shortly."
_SESSION_ENDED_MESSAGE = "Sign in again."

# AAD-SEC-003: Google publishes its signing keys here — PyJWKClient fetches
# and caches them itself (`lifespan` below), so this is looked up once per
# cache window rather than once per sign-in.
_GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
_GOOGLE_ISSUERS = ["accounts.google.com", "https://accounts.google.com"]

# One client, process-lifetime, exactly like the transport it replaces was
# meant to be reused — but this one actually caches: `cache_keys=True` with
# an hour-long `lifespan` means the JWKS fetch happens roughly once an hour,
# not on every single sign-in, and `timeout=5` bounds that fetch when it
# does happen — replacing the previous library's 120-second default with
# something that can no longer take a worker offline for two minutes.
_jwks_client = jwt.PyJWKClient(_GOOGLE_JWKS_URL, cache_keys=True, lifespan=3600, timeout=5)


def _decode_google_id_token(id_token: str, *, audiences: list[str]) -> dict[str, Any]:
    """Synchronous by construction — this is the one function in the whole
    call path that can block on network I/O (the JWKS fetch, when the cache
    is cold), so `verify_google_and_login` always runs it through
    `asyncio.to_thread` rather than calling it directly from async code.
    Once the JWKS is cached, this is pure local crypto: no network call at
    all, verified against keys already held in process memory.
    """
    signing_key = _jwks_client.get_signing_key_from_jwt(id_token)
    return jwt.decode(
        id_token,
        signing_key.key,
        algorithms=["RS256"],
        audience=audiences,
        issuer=_GOOGLE_ISSUERS,
    )


class AuthService:
    def __init__(self, users: UserRepository, refresh_tokens: RefreshTokenRepository) -> None:
        self.users = users
        self.refresh_tokens = refresh_tokens

    async def verify_google_and_login(self, id_token: str) -> tuple[TokenPair, UserProfile]:
        """Verify a Google-issued ID token and sign the person in, creating
        an account on first sign-in.

        AAD-SEC-003: verification is local — against Google's own JWKS,
        cached by `_jwks_client` — rather than delegated to a library that
        made a fresh, blocking, 120-second-timeout HTTPS call on every
        single sign-in. The one check no library can do for us is still
        done explicitly: that the token was actually minted for *this* app
        (the audience check below), not some other app entirely.
        """
        accepted_audiences = [
            aud for aud in (settings.google_web_client_id, settings.google_android_client_id) if aud
        ]
        if not accepted_audiences:
            log.error("google sign-in attempted with no client IDs configured")
            raise Unauthorized(_GOOGLE_FAILURE_MESSAGE)

        try:
            claims = await asyncio.to_thread(
                _decode_google_id_token, id_token, audiences=accepted_audiences
            )
        except jwt.PyJWKClientConnectionError as exc:
            # AAD-SEC-012: this is the failure `AAD-SEC-003` made the *most
            # likely* one — a cold-cache fetch to Google's own servers that
            # times out or can't connect. It is not the user's fault and
            # retrying will probably work; telling them "wrong credentials"
            # would be a lie, and it would make a Google-side or network
            # incident invisible in the error rate (zero 5xx, all 401s).
            log.exception("could not reach google to verify sign-in")
            raise UpstreamError(_GOOGLE_UPSTREAM_MESSAGE) from exc
        except jwt.InvalidTokenError as exc:
            # Forged signature, expired token, wrong audience/issuer, garbage
            # input — all genuinely the token's fault. Logged with the
            # exception type (never the token itself) so a spike in a
            # specific failure mode is visible, unlike the previous single
            # static log line that made every cause indistinguishable.
            log.warning("google id token rejected", extra={"error_type": type(exc).__name__})
            raise Unauthorized(_GOOGLE_FAILURE_MESSAGE) from None
        except jwt.PyJWKClientError as exc:
            # Couldn't resolve a signing key for this token's `kid` — most
            # often a malformed or foreign token, occasionally a JWKS
            # response Google served that didn't parse. Not the clean
            # network-failure signal above, but still not proven-malicious;
            # 401 is the safe default, logged with its type for the same
            # reason as above.
            log.warning(
                "google id token key resolution failed", extra={"error_type": type(exc).__name__}
            )
            raise Unauthorized(_GOOGLE_FAILURE_MESSAGE) from None

        if claims.get("aud") not in accepted_audiences:
            log.warning("google id token had an unexpected audience")
            raise Unauthorized(_GOOGLE_FAILURE_MESSAGE)

        sub = claims.get("sub")
        if not sub:
            raise Unauthorized(_GOOGLE_FAILURE_MESSAGE)

        # AAD-SEC-011: Google issues ID tokens for accounts whose email is
        # not verified (notably Workspace accounts on custom domains).
        # `google_sub` is the account key, so this isn't directly an
        # account-takeover path today — but storing an unverified address
        # as if it were fact is exactly the kind of landmine that becomes
        # one the moment anyone adds email-based lookup, linking, or
        # notifications. Store `None` rather than an address nobody's
        # actually confirmed belongs to this person.
        email = claims.get("email") if claims.get("email_verified") is True else None

        user = await self.users.get_or_create_by_google(
            google_sub=sub,
            email=email,
            name=claims.get("name", ""),
        )
        return await self._issue_tokens(user), self._to_profile(user)

    async def refresh(self, refresh_token: str) -> TokenPair:
        """AAD-SEC-002: rotate-on-use with reuse detection, the standard
        OAuth 2.0 BCP pattern. A refresh token works exactly once — using it
        immediately revokes it and mints a replacement. Presenting an
        already-used token again is not treated as an expired session; it is
        treated as a stolen-token signal, because in normal operation it can
        only mean one of two things: a genuine race (harmless, and rare
        enough not to special-case) or a copy of this token being replayed
        from somewhere it shouldn't be. Either way, the safe response is the
        same — revoke every live session this user has and force a fresh
        sign-in — so the two cases don't need to be told apart.
        """
        payload = decode_token(refresh_token, expected_type="refresh")
        jti = payload.get("jti")
        user_id = payload["sub"]
        if not jti:
            raise Unauthorized(_SESSION_ENDED_MESSAGE)

        row = await self.refresh_tokens.get(jti)
        if row is None:
            raise Unauthorized(_SESSION_ENDED_MESSAGE)

        if row["revoked_at"] is not None:
            if row["replaced_by"] is not None:
                # This exact token was already rotated once — being handed
                # it again is the reuse signal. Revoke the whole family:
                # whoever has the *current* token now finds it dead too, and
                # must sign in again, same as the thief.
                log.warning(
                    "refresh token reuse detected — revoking the session family",
                    extra={"user_id": user_id},
                )
                await self.refresh_tokens.revoke_all_for_user(user_id)
            raise Unauthorized(_SESSION_ENDED_MESSAGE)

        if row["token_hash"] != hash_token(refresh_token):
            # Should not be reachable if the JWT signature verified and the
            # jti matched — kept as defense in depth, and treated the same
            # as any other invalid presentation rather than trusted.
            log.warning("refresh token hash mismatch", extra={"user_id": user_id})
            raise Unauthorized(_SESSION_ENDED_MESSAGE)

        user = await self.users.get_by_id(user_id)
        if not user or user.get("status") != UserStatus.ACTIVE.value:
            raise Unauthorized(_SESSION_ENDED_MESSAGE)

        new_jti = new_refresh_token_id()
        rotated = await self.refresh_tokens.mark_rotated(jti, replaced_by=new_jti)
        if not rotated:
            # Lost a race against a concurrent refresh (or logout) of the
            # exact same token. Safer to fail closed than to hand out a
            # second valid pair for a token that's already being replaced.
            raise Unauthorized(_SESSION_ENDED_MESSAGE)

        return await self._issue_tokens(user, jti=new_jti)

    async def logout(self, refresh_token: str) -> None:
        """Revoke one session. Deliberately lenient about the token itself —
        an already-expired or malformed token has nothing left to revoke,
        and a client calling this during sign-out should never see an error
        for a token it's about to discard anyway."""
        try:
            payload = decode_token(refresh_token, expected_type="refresh")
        except Unauthorized:
            return
        jti = payload.get("jti")
        if jti:
            await self.refresh_tokens.revoke(jti)

    async def logout_all(self, user_id: str) -> None:
        """Revoke every live session for this user — the "sign out of
        everywhere" / "I think my account is compromised" button AAD-SEC-002
        asked for. Access tokens already in flight still work until they
        naturally expire (up to 30 minutes) — only refresh is checked
        against this store — but no session can renew past that point."""
        await self.refresh_tokens.revoke_all_for_user(user_id)

    async def get_profile(self, user_id: str) -> UserProfile:
        user = await self.users.get_by_id(user_id)
        if not user:
            raise Unauthorized(_SESSION_ENDED_MESSAGE)
        return self._to_profile(user)

    async def _issue_tokens(self, user: dict, *, jti: str | None = None) -> TokenPair:
        jti = jti or new_refresh_token_id()
        refresh_token = issue_refresh_token(user["id"], jti=jti)
        await self.refresh_tokens.create(
            jti=jti,
            user_id=user["id"],
            token_hash=hash_token(refresh_token),
            expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_ttl_days),
        )
        return TokenPair(
            access_token=issue_access_token(user["id"], role=user.get("role", "customer")),
            refresh_token=refresh_token,
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
