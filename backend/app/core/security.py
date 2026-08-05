"""Token issuing/verification and one-way hashing.

Two token types are issued, and they are not interchangeable: a short-lived
`access` token used as a bearer credential, and a long-lived `refresh` token
that can only be exchanged at the refresh endpoint. The `typ` claim is checked
on every verification so a stolen refresh token cannot be replayed as an
access token.
"""

from __future__ import annotations

import hmac
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.core.config import settings
from app.core.errors import Unauthorized

TokenType = Literal["access", "refresh"]

_hasher = PasswordHasher()


def hash_secret(raw: str) -> str:
    """Hash an OTP or password. Never store either in plaintext."""
    return _hasher.hash(raw)


def verify_secret(hashed: str, raw: str) -> bool:
    try:
        return _hasher.verify(hashed, raw)
    except (VerifyMismatchError, Exception):  # noqa: B014 - argon2 raises several types
        return False


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def _issue(subject: str, token_type: TokenType, ttl: timedelta, **claims: Any) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": subject,
        "typ": token_type,
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
        **claims,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def issue_access_token(user_id: str, *, role: str = "customer") -> str:
    return _issue(
        user_id, "access", timedelta(minutes=settings.access_token_ttl_min), role=role
    )


def issue_refresh_token(user_id: str) -> str:
    return _issue(user_id, "refresh", timedelta(days=settings.refresh_token_ttl_days))


def decode_token(token: str, *, expected_type: TokenType) -> dict[str, Any]:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise Unauthorized("Your session has expired. Sign in again.") from exc
    except jwt.InvalidTokenError as exc:
        raise Unauthorized("Invalid credentials.") from exc

    if payload.get("typ") != expected_type:
        raise Unauthorized("Invalid credentials.")
    if not payload.get("sub"):
        raise Unauthorized("Invalid credentials.")
    return payload
