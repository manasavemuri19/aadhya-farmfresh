from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PushToken


class PushTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def register(self, *, user_id: str, token: str, platform: str) -> None:
        """Insert a new token, or repoint an existing one at this user.

        Keyed by the token itself (see the 0007 migration docstring) — a
        fresh install that gets the OS's cached token back, or the same
        device signing in as a different account, both just update the
        existing row rather than erroring or duplicating.
        """
        stmt = insert(PushToken).values(user_id=user_id, token=token, platform=platform)
        stmt = stmt.on_conflict_do_update(
            index_elements=[PushToken.token],
            set_={"user_id": user_id, "platform": platform},
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
