from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.ids import new_user_id
from app.db.models import Address as AddressRow
from app.db.models import User as UserRow
from app.domain.enums import Role


def _to_dict(user: UserRow) -> dict[str, Any]:
    return {
        "id": user.id,
        "phone": user.phone,
        "email": user.email,
        "google_sub": user.google_sub,
        "name": user.name,
        "role": user.role,
        "addresses": [
            {
                "label": a.label,
                "line1": a.line1,
                "line2": a.line2,
                "landmark": a.landmark,
                "city": a.city,
                "pincode": a.pincode,
                "latitude": a.latitude,
                "longitude": a.longitude,
            }
            for a in user.addresses
        ],
    }


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, user_id: str) -> dict[str, Any] | None:
        stmt = (
            select(UserRow)
            .options(selectinload(UserRow.addresses))
            .where(UserRow.id == user_id)
        )
        row = (await self.session.execute(stmt)).scalars().first()
        return _to_dict(row) if row else None

    async def get_by_phone(self, phone: str) -> dict[str, Any] | None:
        stmt = (
            select(UserRow)
            .options(selectinload(UserRow.addresses))
            .where(UserRow.phone == phone)
        )
        row = (await self.session.execute(stmt)).scalars().first()
        return _to_dict(row) if row else None

    async def get_by_google_sub(self, google_sub: str) -> dict[str, Any] | None:
        stmt = (
            select(UserRow)
            .options(selectinload(UserRow.addresses))
            .where(UserRow.google_sub == google_sub)
        )
        row = (await self.session.execute(stmt)).scalars().first()
        return _to_dict(row) if row else None

    async def get_or_create_by_phone(self, phone: str) -> dict[str, Any]:
        """Insert-or-ignore on the unique phone column.

        `ON CONFLICT DO NOTHING` makes two simultaneous verifications of the
        same number produce exactly one user, without an application-level lock.
        """
        await self.session.execute(
            insert(UserRow)
            .values(
                id=new_user_id(),
                phone=phone,
                name="",
                role=Role.CUSTOMER.value,
                last_login_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing(index_elements=[UserRow.phone])
        )
        await self.session.flush()

        user = await self.get_by_phone(phone)
        assert user is not None  # guaranteed: we just inserted or it existed
        return user

    async def get_or_create_by_google(
        self, *, google_sub: str, email: str, name: str
    ) -> dict[str, Any]:
        """Same insert-or-ignore shape as phone sign-in, keyed on google_sub
        instead. `name` is only applied on first creation — an existing
        account's name (which the person may have since edited in-app) is
        never silently overwritten by whatever their Google profile says.
        """
        await self.session.execute(
            insert(UserRow)
            .values(
                id=new_user_id(),
                google_sub=google_sub,
                email=email,
                name=name,
                role=Role.CUSTOMER.value,
                last_login_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing(index_elements=[UserRow.google_sub])
        )
        await self.session.flush()

        user = await self.get_by_google_sub(google_sub)
        assert user is not None
        return user

    async def update_profile(self, user_id: str, changes: dict[str, Any]) -> None:
        row = await self.session.get(UserRow, user_id)
        if row is None:
            return
        if changes.get("name") is not None:
            row.name = changes["name"]
        if changes.get("phone") is not None:
            row.phone = changes["phone"]

    async def upsert_address(self, user_id: str, address: dict[str, Any]) -> None:
        """Replace the address with the same label, otherwise add it."""
        stmt = select(AddressRow).where(
            AddressRow.user_id == user_id, AddressRow.label == address["label"]
        )
        row = (await self.session.execute(stmt)).scalars().first()
        if row is None:
            self.session.add(AddressRow(user_id=user_id, **address))
            return
        for field, value in address.items():
            setattr(row, field, value)
