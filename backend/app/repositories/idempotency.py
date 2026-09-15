"""Idempotency keys for order creation.

The unique constraint on (user_id, key) is the entire mechanism: the first
insert wins, and a duplicate raises, which tells the caller this is a retry.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import IdempotencyKey

RETENTION = timedelta(hours=24)


class IdempotencyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def claim(self, user_id: str, key: str, fingerprint: str) -> dict | None:
        """Claim the key. Returns None if the caller owns it and may proceed,
        or the stored record when this is a retry.

        The insert runs in a SAVEPOINT so a duplicate-key violation does not
        poison the surrounding transaction — without that nesting, the whole
        request's transaction would be aborted and unusable.
        """
        now = datetime.now(UTC)
        try:
            async with self.session.begin_nested():
                self.session.add(
                    IdempotencyKey(
                        user_id=user_id,
                        key=key,
                        fingerprint=fingerprint,
                        status="in_progress",
                        response=None,
                        created_at=now,
                        expires_at=now + RETENTION,
                    )
                )
            return None
        except IntegrityError:
            stmt = select(IdempotencyKey).where(
                IdempotencyKey.user_id == user_id, IdempotencyKey.key == key
            )
            row = (await self.session.execute(stmt)).scalars().first()
            if row is None:
                return {"status": "in_progress", "response": None, "fingerprint": None}
            return {
                "status": row.status,
                "response": row.response,
                "fingerprint": row.fingerprint,
            }

    async def complete(self, user_id: str, key: str, response: dict[str, Any]) -> None:
        await self.session.execute(
            update(IdempotencyKey)
            .where(IdempotencyKey.user_id == user_id, IdempotencyKey.key == key)
            .values(status="completed", response=response)
        )

    async def release(self, user_id: str, key: str) -> None:
        """Drop an in-progress key after a failure so the customer can retry."""
        await self.session.execute(
            delete(IdempotencyKey).where(
                IdempotencyKey.user_id == user_id,
                IdempotencyKey.key == key,
                IdempotencyKey.status == "in_progress",
            )
        )

    async def delete_expired(self, *, batch_size: int = 500) -> int:
        """AAD-REL-003: this used to be one bare, unbounded `DELETE`. After a
        backlog builds — a sweeper that's been down, a deploy storm — that
        single statement would try to delete everything at once, taking a
        long lock and blocking live traffic for the duration. Deleting by a
        LIMIT-bounded id subquery in a loop keeps every individual statement
        small and fast, at the cost of needing several round trips instead
        of one; `ix_idempotency_expires` makes each subquery itself cheap.
        """
        total = 0
        while True:
            batch = select(IdempotencyKey.id).where(
                IdempotencyKey.expires_at < datetime.now(UTC)
            ).limit(batch_size)
            result = await self.session.execute(
                delete(IdempotencyKey).where(IdempotencyKey.id.in_(batch))
            )
            deleted = result.rowcount or 0
            total += deleted
            if deleted < batch_size:
                return total
