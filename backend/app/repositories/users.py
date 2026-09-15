from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import Conflict
from app.core.ids import new_user_id
from app.db.models import Address as AddressRow
from app.db.models import User as UserRow
from app.domain.enums import Role
from app.domain.geo import haversine_km

# AAD-SEC-010: "10 is generous" per the audit's own suggestion — plenty for
# home/work/a couple of relatives' addresses, nowhere near enough to matter
# for the eager `selectin` load on every user read.
MAX_ADDRESSES_PER_USER = 10

# AAD-SEC-028: a delivery bike or car in Hyderabad traffic does not sustain
# this speed between two GPS fixes. Deliberately generous — well above any
# real delivery, including a fast highway stretch — since the cost of a
# false positive (a real report gets flagged) is just noise in a field
# nothing currently blocks on, while the cost of a false negative (a
# spoofed jump goes unflagged) is silent. Flagged, not rejected: the update
# is still stored, since the app has no retry UX for a refused report today
# and GPS drift after an idle period can look identical to a genuine jump.
_MAX_PLAUSIBLE_SPEED_KMH = 120.0
# Below this interval, any distance implies an enormous — and mostly
# meaningless — speed (a phone can send two updates a second apart on a
# flaky connection's retry). Skip the plausibility check rather than flag
# on noise the interval itself explains.
_MIN_CHECK_INTERVAL_SECONDS = 5.0

log = logging.getLogger(__name__)


def _to_dict(user: UserRow) -> dict[str, Any]:
    return {
        "id": user.id,
        "phone": user.phone,
        "email": user.email,
        "google_sub": user.google_sub,
        "name": user.name,
        "role": user.role,
        "status": user.status,
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

    def _loaded(self, stmt):
        """Eager-load addresses and refresh anything already in the identity
        map. Same reasoning as the equivalent helper in orders.py: a request
        that both mutates a user (e.g. adding an address) and then reads it
        back for the response would otherwise get a stale, pre-mutation
        object from SQLAlchemy's identity map — the profile endpoint
        genuinely returned an empty address list immediately after saving
        one, confirmed live, until this was added.
        """
        return stmt.options(selectinload(UserRow.addresses)).execution_options(
            populate_existing=True
        )

    async def get_by_id(self, user_id: str) -> dict[str, Any] | None:
        stmt = self._loaded(select(UserRow)).where(UserRow.id == user_id)
        row = (await self.session.execute(stmt)).scalars().first()
        return _to_dict(row) if row else None

    async def get_by_phone(self, phone: str) -> dict[str, Any] | None:
        stmt = self._loaded(select(UserRow)).where(UserRow.phone == phone)
        row = (await self.session.execute(stmt)).scalars().first()
        return _to_dict(row) if row else None

    async def get_by_google_sub(self, google_sub: str) -> dict[str, Any] | None:
        stmt = self._loaded(select(UserRow)).where(UserRow.google_sub == google_sub)
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
        self, *, google_sub: str, email: str | None, name: str
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
        # Flushed, not just set on the in-memory object: this session has
        # autoflush off, and update_me (auth.py) re-reads the profile via a
        # fresh SELECT in the same request to build its response. Without an
        # explicit flush here, that SELECT would see the database's
        # pre-update row — the in-memory change would look "lost" in the
        # response even though it commits fine at the end of the request.
        await self.session.flush()

    async def upsert_address(self, user_id: str, address: dict[str, Any]) -> None:
        """Replace the address with the same label, otherwise add it.

        AAD-SEC-010: previously a check-then-act (SELECT, then conditionally
        INSERT) — safe from duplicate rows thanks to the `uq_address_user_label`
        constraint, but the losing side of a concurrent save of the *same*
        label got an unhandled `IntegrityError`, a 500 to the customer. Now
        a single atomic `INSERT ... ON CONFLICT (user_id, label) DO UPDATE`:
        there is no losing side, because both statements just resolve to the
        same final row.

        A *new* label is capped at MAX_ADDRESSES_PER_USER per account —
        updating an existing label never counts against the cap, since it
        doesn't add a row. The count-then-insert check below is not
        perfectly race-proof against two concurrent *new* labels both
        landing right at the cap (this is an anti-abuse limit, not a
        correctness invariant the way the unique constraint is — the
        storage- and read-amplification abuse this closes is a script
        creating rows far faster than any real concurrent-request race would
        ever hit it), and it stays cheap: one extra indexed count query, only
        on the new-label path.
        """
        existing_stmt = select(AddressRow.id).where(
            AddressRow.user_id == user_id, AddressRow.label == address["label"]
        )
        existing_id = (await self.session.execute(existing_stmt)).scalars().first()

        if existing_id is None:
            count_stmt = select(func.count()).select_from(AddressRow).where(
                AddressRow.user_id == user_id
            )
            current_count = (await self.session.execute(count_stmt)).scalar_one()
            if current_count >= MAX_ADDRESSES_PER_USER:
                raise Conflict(
                    f"You can save up to {MAX_ADDRESSES_PER_USER} addresses. "
                    "Remove one before adding another."
                )

        stmt = (
            insert(AddressRow)
            .values(user_id=user_id, **address)
            .on_conflict_do_update(
                index_elements=[AddressRow.user_id, AddressRow.label],
                set_=address,
            )
        )
        await self.session.execute(stmt)
        await self.session.flush()

    # ---------- delivery agent location ----------
    # Isolated here rather than in a delivery-specific repository since these
    # columns live on `users`, same as everything else this repo touches.

    async def get_agent_location(
        self, user_id: str
    ) -> tuple[float, float] | None:
        row = await self.session.get(UserRow, user_id)
        if row is None or row.last_lat is None or row.last_lng is None:
            return None
        return (row.last_lat, row.last_lng)

    async def update_agent_location(
        self, user_id: str, *, latitude: float, longitude: float
    ) -> bool:
        """Writes the new position unconditionally, and returns whether this
        update was flagged (AAD-SEC-028): implausible if the implied speed
        from the previous reading exceeds `_MAX_PLAUSIBLE_SPEED_KMH`, over an
        interval long enough that the speed figure actually means something
        (`_MIN_CHECK_INTERVAL_SECONDS`). The bounding-box check (are these
        coordinates even in Hyderabad) lives in the schema layer
        (`AgentLocationUpdate`) — this is the second, independent layer: is
        this move from the *previous* position physically plausible.
        """
        previous = await self.get_agent_location_with_time(user_id)
        flagged = False
        now = datetime.now(UTC)
        if previous is not None:
            prev_lat, prev_lng, prev_at = previous
            elapsed_seconds = (now - prev_at).total_seconds()
            if elapsed_seconds >= _MIN_CHECK_INTERVAL_SECONDS:
                distance_km = haversine_km(prev_lat, prev_lng, latitude, longitude)
                speed_kmh = distance_km / (elapsed_seconds / 3600)
                flagged = speed_kmh > _MAX_PLAUSIBLE_SPEED_KMH

        await self.session.execute(
            update(UserRow)
            .where(UserRow.id == user_id)
            .values(
                last_lat=latitude, last_lng=longitude, last_location_at=now,
                last_location_flagged=flagged,
            )
        )
        if flagged:
            log.warning(
                "agent_location_jump_flagged",
                extra={"user_id": user_id, "latitude": latitude, "longitude": longitude},
            )
        return flagged

    async def get_agent_location_with_time(
        self, user_id: str
    ) -> tuple[float, float, datetime] | None:
        """Same data as get_agent_location, plus the freshness timestamp —
        kept as a separate method rather than changing get_agent_location's
        return shape, since that one is unpacked positionally as exactly two
        floats in DeliveryService.list_requests."""
        row = await self.session.get(UserRow, user_id)
        if (
            row is None
            or row.last_lat is None
            or row.last_lng is None
            or row.last_location_at is None
        ):
            return None
        return (row.last_lat, row.last_lng, row.last_location_at)

    async def list_delivery_agent_ids(self) -> list[str]:
        """Every delivery agent, regardless of location — used only to fan
        out a "new order available" push. The in-app Requests list still
        does the real distance filtering (see DeliveryService.list_requests);
        a push is just an attention-getter, not the source of truth for who
        can take the job."""
        stmt = select(UserRow.id).where(UserRow.role == Role.DELIVERY_AGENT.value)
        return list((await self.session.execute(stmt)).scalars().all())
