"""OTP challenge storage.

Codes are Argon2-hashed, attempts are counted server-side, and expired rows are
deleted by the background sweeper. Postgres has no TTL index, so that cleanup
is explicit — `delete_expired` is called on the same timer that reclaims
abandoned checkouts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import hash_secret, verify_secret
from app.db.models import OtpChallenge


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class OtpRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _latest(self, phone: str) -> OtpChallenge | None:
        stmt = (
            select(OtpChallenge)
            .where(OtpChallenge.phone == phone)
            .order_by(OtpChallenge.created_at.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def create(self, phone: str, code: str) -> datetime:
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=settings.otp_ttl_seconds)
        # One live challenge per phone: a new code invalidates the previous one.
        await self.session.execute(
            delete(OtpChallenge).where(OtpChallenge.phone == phone)
        )
        self.session.add(
            OtpChallenge(
                phone=phone,
                code_hash=hash_secret(code),
                attempts=0,
                created_at=now,
                expires_at=expires_at,
            )
        )
        await self.session.flush()
        return expires_at

    async def verify_and_consume(self, phone: str, code: str) -> tuple[bool, str]:
        """Match a code against the live challenge for this phone.

        A successful match is **not** deleted immediately. If the client's
        connection drops after the server has already committed success — a
        slow network, a dropped connection, anything — the client sees a
        failure and, quite reasonably, retries with the exact same code. Under
        the old delete-on-success behaviour that retry hit an already-gone row
        and came back "invalid code", even though the account had genuinely
        just been signed in. That combination (a scary error, followed by
        already being signed in on the next launch) was confusing and wrong.
        Keeping the row and allowing the identical correct code to succeed
        again within a short grace window makes verification idempotent for
        that case, the same way a checkout idempotency key does for orders.
        A *different* code after consumption still fails — this only forgives
        an exact repeat of the one that already worked.
        """
        challenge = await self._latest(phone)
        if challenge is None:
            return False, "no_challenge"

        if _aware(challenge.expires_at) < datetime.now(UTC):
            await self.session.execute(
                delete(OtpChallenge).where(OtpChallenge.phone == phone)
            )
            return False, "expired"

        if challenge.consumed_at is not None:
            # Already used. Only a replay of the identical code, within the
            # grace window, is allowed through — anything else is a genuine
            # failure (including a correct-looking code after the window
            # closes, which is intentional: grace is for retries, not reuse).
            grace_expired = (
                datetime.now(UTC) - _aware(challenge.consumed_at)
            ) > timedelta(seconds=settings.otp_consumed_grace_seconds)
            if grace_expired or not verify_secret(challenge.code_hash, code):
                return False, "no_challenge"
            return True, "ok"

        if challenge.attempts >= settings.otp_max_attempts:
            await self.session.execute(
                delete(OtpChallenge).where(OtpChallenge.phone == phone)
            )
            return False, "too_many_attempts"

        if not verify_secret(challenge.code_hash, code):
            challenge.attempts += 1
            await self.session.flush()
            return False, "mismatch"

        challenge.consumed_at = datetime.now(UTC)
        await self.session.flush()
        return True, "ok"

    async def seconds_until_resend_allowed(self, phone: str) -> int:
        challenge = await self._latest(phone)
        if challenge is None:
            return 0
        elapsed = (datetime.now(UTC) - _aware(challenge.created_at)).total_seconds()
        return max(0, int(settings.otp_resend_cooldown_seconds - elapsed))

    async def delete_expired(self) -> int:
        result = await self.session.execute(
            delete(OtpChallenge).where(OtpChallenge.expires_at < datetime.now(UTC))
        )
        return result.rowcount or 0
