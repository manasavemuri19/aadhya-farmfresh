"""Token issuing/verification.

Two token types are issued, and they are not interchangeable: a short-lived
`access` token used as a bearer credential, and a long-lived `refresh` token
that can only be exchanged at the refresh endpoint. The `typ` claim is checked
on every verification so a stolen refresh token cannot be replayed as an
access token.
"""

from __future__ import annotations

import hmac
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from app.core.config import settings
from app.core.errors import Unauthorized

TokenType = Literal["access", "refresh"]

log = logging.getLogger(__name__)

# AAD-SEC-016: `hash_secret`/`verify_secret` used to live here — Argon2
# hashing for the old phone/OTP-login subsystem's stored codes. AAD-QUAL-001
# deleted that whole subsystem (dead, dangerous authentication surface), which
# left these two functions with zero callers anywhere in the app or the tests.
# Reintroduced for AAD-SEC-027 (in-app delivery-verification OTP), following
# the reintroduction guidance that finding's own deletion comment left
# behind: catch Argon2's own exception types specifically rather than a bare
# `except Exception`, and call `check_needs_rehash()` on every successful
# verify so parameters can be upgraded later without a forced re-hash
# migration. A single module-level `PasswordHasher()` is reused across calls
# — it's stateless aside from its (fixed, config-derived) hashing
# parameters, so there's nothing per-call to isolate.
_hasher = PasswordHasher()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def hash_secret(secret: str) -> str:
    """Argon2-hash a short-lived secret (e.g. a delivery-verification code)
    for storage. Never store `secret` itself alongside this — the hash is
    the only thing verification should ever need to trust."""
    return _hasher.hash(secret)


def verify_secret(secret: str, hashed: str) -> bool:
    """True iff `secret` matches the Argon2 hash previously produced by
    `hash_secret`. `InvalidHashError` covers a malformed/foreign hash value
    (defensive — nothing in this codebase should ever store one) so this
    never raises on bad input, only ever returns False."""
    try:
        _hasher.verify(hashed, secret)
    except (VerifyMismatchError, InvalidHashError):
        return False
    if _hasher.check_needs_rehash(hashed):
        log.info("secret_hash_needs_rehash")
    return True


def _issue(subject: str, token_type: TokenType, ttl: timedelta, **claims: Any) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": subject,
        "typ": token_type,
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
        **claims,
    }
    return jwt.encode(
        payload, settings.jwt_secret.get_secret_value(), algorithm=settings.jwt_algorithm
    )


def issue_access_token(user_id: str, *, role: str = "customer") -> str:
    return _issue(
        user_id, "access", timedelta(minutes=settings.access_token_ttl_min), role=role
    )


def issue_refresh_token(user_id: str, *, jti: str) -> str:
    # AAD-SEC-002: `jti` is what lets a single refresh token be looked up,
    # rotated and revoked server-side — see RefreshTokenRepository. Required,
    # not optional: every refresh token issued must be trackable, or the
    # revocation store this claim exists for has nothing to key on.
    return _issue(user_id, "refresh", timedelta(days=settings.refresh_token_ttl_days), jti=jti)


def decode_token(token: str, *, expected_type: TokenType) -> dict[str, Any]:
    try:
        payload = jwt.decode(
            token, settings.jwt_secret.get_secret_value(), algorithms=[settings.jwt_algorithm]
        )
    except jwt.ExpiredSignatureError as exc:
        raise Unauthorized("Your session has expired. Sign in again.") from exc
    except jwt.InvalidTokenError as exc:
        raise Unauthorized("Invalid credentials.") from exc

    if payload.get("typ") != expected_type:
        raise Unauthorized("Invalid credentials.")
    if not payload.get("sub"):
        raise Unauthorized("Invalid credentials.")
    return payload
