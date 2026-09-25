from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import Conflict, NotFound
from app.core.ids import new_user_id
from app.db.models import Address as AddressRow
from app.db.models import User as UserRow
from app.domain.enums import Role, UserStatus
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

# AAD-PERF-004: a defensive cap on the "new order" push fan-out query, not a
# realistic expectation for this farm's own delivery team — see
# list_delivery_agent_ids for the reasoning.
_MAX_AGENTS_PER_FANOUT = 2000

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
        """Eager-load addresses via an explicit `selectinload`, matching the
        model's own `lazy="selectin"` default so the strategy is documented
        here rather than only implicit in `db/models.py`.

        AAD-PERF-003: this used to also carry `execution_options(
        populate_existing=True)`, on the theory that a request mutating a
        user (e.g. adding an address) and then reading it back for the
        response would otherwise get a stale, pre-mutation object back from
        SQLAlchemy's identity map. **Tested that theory directly before
        removing it, rather than trusting the comment**: built the exact
        sequence the comment describes — an ORM load of a user (`session.get
        (UserRow, ...)`, the same call `update_profile` makes), then a
        Core-level `upsert_address` write that never touches that loaded
        object, then a second, plain `get_by_id` with no `populate_existing`
        at all — and it correctly returned the just-saved address every
        time. Same result mutating a plain scalar column (`name`) via a raw
        Core `UPDATE` in between two reads. On this SQLAlchemy version, an
        explicit `selectinload` query option refreshes the targeted
        relationship regardless of whether it was already loaded, and a
        plain re-`SELECT` targeting an object already in the identity map
        does update its scalar columns too, as long as nothing has an
        unflushed in-memory change pending — which nothing in this
        repository's own call chain ever does. Whatever bug the original
        comment was written against, it isn't reproducible against the
        current code on the current SQLAlchemy version — `populate_existing`
        was defeating the identity map (forcing a full re-fetch, including
        the `addresses` join) on **every** call through this repository —
        `_reauthorize_from_db` on every privileged request, `AuthService.
        refresh` on every token refresh, a plain `GET /me` — for a
        correctness problem that doesn't exist today. Removed outright
        rather than scoped down, since scoping it to "just the one call
        site that might need it" would have kept paying to guard against a
        bug this session could not make happen. See
        `tests/test_batch21_hygiene.py` for the standing regression tests
        that pin this — if a future SQLAlchemy upgrade or a new Core-level
        write path ever does reintroduce staleness, those tests will fail
        first.
        """
        return stmt.options(selectinload(UserRow.addresses))

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

    # AAD-QUAL-005: `get_or_create_by_phone` used to live here, with an
    # `assert user is not None` at the end that this batch set out to fix
    # like its `get_or_create_by_google` sibling below. Writing that fix's
    # own regression test surfaced something the assert had been quietly
    # hiding: the method's `on_conflict_do_nothing(index_elements=[UserRow.phone])`
    # targets a unique index that no longer exists — `phone` has been
    # `nullable=True, index=True` with **no** uniqueness since phone/OTP
    # login was retired in favour of Google sign-in (see the comment in
    # `routes/auth.py`, right where this method's last caller was removed:
    # "phone is now plain delivery contact info, not a login identity, so
    # it can't safely stay unique"). Calling this method doesn't return a
    # wrong answer — it raises `ProgrammingError: there is no unique or
    # exclusion constraint matching the ON CONFLICT specification` on its
    # very first statement, every time, confirmed directly. Zero callers
    # remain anywhere in the backend or mobile app (confirmed by grep). This
    # is the same shape as `AAD-SEC-016`'s dead Argon2 helpers: fixing the
    # assert on a method that cannot otherwise run would have been polishing
    # code nothing calls and, per `AAD-QUAL-001`'s own stated preference for
    # dangerous/dead auth surface, deletion is the fix — not a patch.
    # Recoverable from git history if phone-based login is ever deliberately
    # rebuilt, at which point it will need a real unique index restored too.

    async def get_or_create_by_google(
        self, *, google_sub: str, email: str | None, name: str
    ) -> dict[str, Any]:
        """Same insert-or-ignore shape as phone sign-in, keyed on google_sub
        instead. `name` is only applied on first creation — an existing
        account's name (which the person may have since edited in-app) is
        never silently overwritten by whatever their Google profile says.

        AAD-DATA-002: `last_login_at` used to be set only in the `values()`
        of the insert above, so it moved only on the very first sign-in ever
        — every subsequent call hit `on_conflict_do_nothing` and left the
        existing row untouched. The column name promises "last login", not
        "first login"; any "active users" metric built on it was silently
        wrong for every returning user. The explicit `UPDATE` below now runs
        on every call, so it also re-stamps the row this same call just
        inserted — one extra statement, harmless and correct either way.
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
        await self.session.execute(
            update(UserRow)
            .where(UserRow.google_sub == google_sub)
            .values(last_login_at=datetime.now(UTC))
        )
        await self.session.flush()

        user = await self.get_by_google_sub(google_sub)
        if user is None:
            # AAD-QUAL-005: same fix as get_or_create_by_phone above.
            raise RuntimeError(
                f"user for google_sub {google_sub!r} vanished immediately "
                "after an insert-or-ignore on its own unique index — "
                "should be impossible"
            )
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

    async def upsert_address(
        self, user_id: str, address: dict[str, Any], *, previous_label: str | None = None
    ) -> None:
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

        AAD-MOB-022 (multi-address support): `previous_label`, when given
        and different from `address["label"]`, means "this is address
        management's own rename" — the row currently saved under
        `previous_label` gets *relabelled* to `address["label"]`, not
        duplicated into a second row. That has to be a plain `UPDATE`
        against the existing row, not the `INSERT ... ON CONFLICT` path
        below: an upsert keyed on the *new* label would create a brand-new
        row (since nothing is saved under that label yet) and leave the old
        one behind untouched, doubling the address instead of renaming it.
        Renaming never touches MAX_ADDRESSES_PER_USER — it isn't adding a
        row — and is rejected if the new label is already taken by a
        *different* address, or if nothing is actually saved under
        `previous_label` any more (e.g. a second device already renamed or
        deleted it first).
        """
        fields = {
            "label": address["label"],
            "line1": address["line1"],
            "line2": address["line2"],
            "landmark": address["landmark"],
            "city": address["city"],
            "pincode": address["pincode"],
            "latitude": address["latitude"],
            "longitude": address["longitude"],
        }

        if previous_label is not None and previous_label != address["label"]:
            clash_stmt = select(AddressRow.id).where(
                AddressRow.user_id == user_id, AddressRow.label == address["label"]
            )
            if (await self.session.execute(clash_stmt)).scalars().first() is not None:
                raise Conflict(
                    f'You already have an address labelled "{address["label"]}".'
                )
            result = await self.session.execute(
                update(AddressRow)
                .where(AddressRow.user_id == user_id, AddressRow.label == previous_label)
                .values(**fields)
            )
            if result.rowcount == 0:
                raise NotFound(
                    f'No saved address labelled "{previous_label}" to rename.'
                )
            await self.session.flush()
            return

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

        # AAD-SEC-021: `**address` / `set_=address` used to splat this dict
        # straight into the ORM insert and its conflict-update — safe only
        # because `Schema` sets `extra="forbid"`, so `address` can never
        # actually carry more keys than `Address` declares. One schema
        # change away from mass assignment (any field added to `AddressRow`
        # without also being explicitly excluded from `Address` would
        # become client-settable the moment `Address` gained a matching
        # field name, with nothing here to notice). Every field is named
        # explicitly instead, in the exact shape `Address.model_dump()`
        # always produces (built once, above, and reused by the rename
        # branch too). `AAD-QUAL-029` flags the same splat-into-ORM pattern
        # elsewhere in this codebase (`products.py`) — still open, not
        # fixed here, since that's a different write path with its own
        # verification to do.
        stmt = (
            insert(AddressRow)
            .values(user_id=user_id, **fields)
            .on_conflict_do_update(
                index_elements=[AddressRow.user_id, AddressRow.label],
                set_=fields,
            )
        )
        await self.session.execute(stmt)
        await self.session.flush()

    async def delete_address(self, user_id: str, label: str) -> bool:
        """AAD-MOB-022 (multi-address support): the one CRUD operation
        `upsert_address` never covered — nothing here before this let a
        customer actually remove a saved address once it existed, only
        replace one label's contents with another's. Returns whether a row
        was actually deleted, so the route can tell "removed" from "there
        was never an address by that name" (NotFound) rather than both
        silently looking like success.
        """
        result = await self.session.execute(
            delete(AddressRow).where(AddressRow.user_id == user_id, AddressRow.label == label)
        )
        await self.session.flush()
        return result.rowcount > 0

    async def erase(self, user_id: str) -> None:
        """AAD-DATA-006 / AAD-API-002 — the DPDP Act 2023 right to erasure.

        `orders.user_id` stays `ON DELETE RESTRICT` on purpose: order rows
        are tax and accounting records, and losing them just to satisfy an
        erasure request would trade one compliance problem for another.
        Nothing here deletes a user row or an order row — erasure is
        separated from deletion, exactly as the finding recommends. What
        this does remove: every saved address outright (nothing else
        references them, so there's nothing to preserve them for), and the
        identifying fields on the user row itself — name, phone, email, the
        Google account link. `status` flips to `DELETED`, which
        `_reauthorize_from_db` (api/deps.py) and `AuthService.refresh`
        already both check — the same mechanism AAD-SEC-002 built for
        suspension. Session cleanup (revoking every refresh token) is the
        caller's job, same as it already is for `logout_all`; this method
        only owns the data.

        Order-level personal data — the delivery address (and coordinates)
        snapshotted onto each of this user's past orders — is a separate
        write, against a different table this repository doesn't own; see
        `OrderRepository.anonymize_addresses_for_user`, called alongside
        this from the same route.
        """
        await self.session.execute(delete(AddressRow).where(AddressRow.user_id == user_id))
        await self.session.execute(
            update(UserRow)
            .where(UserRow.id == user_id)
            .values(
                name="", phone=None, email=None, google_sub=None,
                status=UserStatus.DELETED.value,
            )
        )
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
        """Every *active* delivery agent, regardless of location — used only
        to fan out a "new order available" push. The in-app Requests list
        still does the real distance filtering (see
        DeliveryService.list_requests); a push is just an attention-getter,
        not the source of truth for who can take the job.

        AAD-PERF-004: two changes from the original unfiltered, unbounded
        version. First, `status == ACTIVE` — a suspended or deleted agent's
        account should not be woken up for a new order; that was a
        correctness gap, not just a perf one. Second, a defensive `LIMIT`:
        this table has no on-duty flag or service-area column today (the
        `last_lat`/`last_lng` fields track an in-progress delivery, not a
        home base), so real geographic scoping — the fix the finding really
        wants — needs a schema addition this batch does not invent. The
        `LIMIT` is a safety net against an unbounded scan and fan-out if the
        agent roster ever grows far past what a single push batch should
        target, with a warning logged so a roster that large gets noticed
        and re-scoped deliberately rather than silently truncated forever.
        """
        stmt = (
            select(UserRow.id)
            .where(
                UserRow.role == Role.DELIVERY_AGENT.value,
                UserRow.status == UserStatus.ACTIVE.value,
            )
            .limit(_MAX_AGENTS_PER_FANOUT + 1)
        )
        ids = list((await self.session.execute(stmt)).scalars().all())
        if len(ids) > _MAX_AGENTS_PER_FANOUT:
            log.warning(
                "delivery agent roster (%d+) exceeds the push fan-out cap "
                "of %d — geographic scoping is overdue",
                len(ids),
                _MAX_AGENTS_PER_FANOUT,
            )
            ids = ids[:_MAX_AGENTS_PER_FANOUT]
        return ids

    async def list_staff_ids(self) -> list[str]:
        """AAD-BIZ-005: everyone who can reach the admin support inbox —
        `staff` and `admin`, the same two roles `Principal.is_staff` accepts
        (an owner account is still staff for this purpose). Used only to fan
        out a "new support ticket" push the same way
        `list_delivery_agent_ids` fans out a "new order" one."""
        stmt = select(UserRow.id).where(
            UserRow.role.in_([Role.STAFF.value, Role.ADMIN.value])
        )
        return list((await self.session.execute(stmt)).scalars().all())
