from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PushToken

# AAD-SEC-032: a token not seen (re-registered or freshly used) in this long
# is either a stale reinstall Expo would have told us about anyway, or —
# the actual point of this finding — a device that signed out and was
# never told to stop delivering. Pruned by the housekeeping sweep alongside
# expired idempotency keys and refresh tokens.
_STALE_AFTER = timedelta(days=90)


class PushTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def register(self, *, user_id: str, token: str, platform: str) -> None:
        """Insert a new token, or repoint an existing one at this user.

        Keyed by the token itself (see the 0007 migration docstring) — a
        fresh install that gets the OS's cached token back, or the same
        device signing in as a different account, both just update the
        existing row rather than erroring or duplicating.

        AAD-SEC-032 side effect: `updated_at` is set explicitly in the
        conflict branch. Same shape as `AAD-DATA-002`'s `last_login_at`
        bug — the column's own `onupdate=func.now()` fires for an ORM or
        `Table.update()` statement, but `on_conflict_do_update`'s `set_`
        clause is compiled as part of the `INSERT`, not an `UPDATE`, so
        nothing would bump it on a repeat registration otherwise. That
        matters here specifically because `prune_stale` below treats a
        fresh `updated_at` as "this device is still active".
        """
        now = datetime.now(UTC)
        stmt = insert(PushToken).values(
            user_id=user_id, token=token, platform=platform, updated_at=now
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[PushToken.token],
            set_={"user_id": user_id, "platform": platform, "updated_at": now},
        )
        await self.session.execute(stmt)

    async def list_for_users(self, user_ids: list[str]) -> list[str]:
        if not user_ids:
            return []
        stmt = select(PushToken.token).where(PushToken.user_id.in_(user_ids))
        return list((await self.session.execute(stmt)).scalars().all())

    async def delete_invalid(self, tokens: list[str]) -> None:
        """Drop tokens Expo reports as permanently dead (app uninstalled,
        token rotated) — see PushService._send."""
        if not tokens:
            return
        await self.session.execute(
            PushToken.__table__.delete().where(PushToken.token.in_(tokens))
        )

    async def delete_for_user(self, *, user_id: str, token: str) -> bool:
        """AAD-SEC-032: called from sign-out. Scoped to `(user_id, token)`
        rather than the token alone — a caller can only ever delete a
        registration that is actually theirs, so this can't be used to
        deregister someone else's device even if the token leaked (the same
        exposure `AAD-SEC-031` is about). Returns whether a row was
        actually deleted, so the route can tell "already gone" apart from a
        real failure without needing to."""
        result = await self.session.execute(
            PushToken.__table__.delete().where(
                PushToken.token == token, PushToken.user_id == user_id
            )
        )
        return result.rowcount > 0

    async def prune_stale(self) -> int:
        """AAD-SEC-032: the other half of the fix — a token deleted at
        sign-out only covers a customer who actually signs out. A shared,
        resold or app-deleted-without-signing-out handset never calls
        `delete_for_user` at all, so this sweeps anything not touched
        (registered *or* re-registered — `register`'s upsert bumps
        `updated_at` on every call, including from the same device signing
        in again) in `_STALE_AFTER`."""
        cutoff = datetime.now(UTC) - _STALE_AFTER
        result = await self.session.execute(
            PushToken.__table__.delete().where(PushToken.updated_at < cutoff)
        )
        return result.rowcount
