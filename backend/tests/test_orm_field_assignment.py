"""AAD-SEC-021 / AAD-QUAL-029 — `upsert_address` and `upsert_category` used
to splat a schema dump straight into an ORM insert/constructor
(`AddressRow(user_id=user_id, **address)`, `set_=address`,
`CategoryRow(**category.model_dump())`). Safe only because `Schema` sets
`extra="forbid"` upstream — one schema change away from mass assignment,
with nothing at this layer to notice. Both now name every field explicitly.

These tests prove two things the existing address/category tests don't
specifically target: every field actually round-trips through the explicit
assignment (not just the ones another test happens to check), and an extra
key the caller's dict might carry is silently ignored rather than either
erroring unpredictably or being written to a column nothing asked for.
"""

from __future__ import annotations

from sqlalchemy import select

from app.db.models import Address as AddressRow
from app.db.models import Category as CategoryRow
from app.repositories.products import ProductRepository
from app.repositories.users import UserRepository
from app.schemas.catalog import Category


def _full_address(**overrides) -> dict:
    base = {
        "label": "Work",
        "line1": "42 Tech Park Road",
        "line2": "Block C, 3rd Floor",
        "landmark": "Near the big water tank",
        "city": "Hyderabad",
        "pincode": "500081",
        "latitude": 17.4401,
        "longitude": 78.3489,
    }
    base.update(overrides)
    return base


async def _make_user(session, sub="orm-field-assignment-user"):
    return await UserRepository(session).get_or_create_by_google(
        google_sub=sub, email=f"{sub}@example.com", name="Test User",
    )


async def _get_work_address(session, user_id: str) -> AddressRow:
    stmt = select(AddressRow).where(AddressRow.user_id == user_id, AddressRow.label == "Work")
    return (await session.execute(stmt)).scalar_one()


class TestUpsertAddressExplicitFields:
    async def test_every_field_round_trips_not_just_the_obvious_ones(self, session):
        user = await _make_user(session)
        await UserRepository(session).upsert_address(user["id"], _full_address())

        row = await _get_work_address(session, user["id"])
        assert row.line1 == "42 Tech Park Road"
        assert row.line2 == "Block C, 3rd Floor"
        assert row.landmark == "Near the big water tank"
        assert row.city == "Hyderabad"
        assert row.pincode == "500081"
        assert row.latitude == 17.4401
        assert row.longitude == 78.3489

    async def test_an_update_to_the_same_label_also_carries_every_field(self, session):
        user = await _make_user(session)
        users = UserRepository(session)
        await users.upsert_address(user["id"], _full_address())
        await users.upsert_address(
            user["id"], _full_address(line1="99 New Address Line", landmark="")
        )

        row = await _get_work_address(session, user["id"])
        assert row.line1 == "99 New Address Line"
        assert row.landmark == ""  # the update really overwrote it, not just added to it

    async def test_a_key_that_is_not_a_declared_address_field_is_silently_ignored(self, session):
        """The mass-assignment scenario the finding warns about, proven at
        this layer directly: even if a dict somehow arrived here carrying
        an extra key, the explicit field list means it's dropped, not
        forwarded to the ORM row. Confirmed by reverting to the old `**address`
        splat during this fix's own verification: the old code didn't quietly
        forward the extra key either — it raised an opaque `CompileError`
        ("Unconsumed column names") instead, which is its own problem (a
        stray key anywhere in the dict crashes the whole request); the
        explicit-fields version is strictly better on both axes, not just
        the mass-assignment one the finding named."""
        user = await _make_user(session)
        address = _full_address()
        address["is_verified_by_staff"] = True  # not a real Address/AddressRow field

        await UserRepository(session).upsert_address(user["id"], address)

        row = await _get_work_address(session, user["id"])
        assert not hasattr(row, "is_verified_by_staff")


class TestUpsertCategoryExplicitFields:
    async def test_a_new_category_gets_every_field_explicitly(self, session):
        await ProductRepository(session).upsert_category(
            Category(slug="new-cat-explicit", name="New Category", sort_order=7, is_active=False)
        )
        # upsert_category doesn't flush itself; this session has autoflush off
        await session.flush()
        row = await session.get(CategoryRow, "new-cat-explicit")
        assert row.name == "New Category"
        assert row.sort_order == 7
        assert row.is_active is False

    async def test_updating_an_existing_category_still_works_unchanged(self, session):
        repo = ProductRepository(session)
        await repo.upsert_category(
            Category(slug="update-cat-explicit", name="Original", sort_order=1, is_active=True)
        )
        # upsert_category doesn't flush itself, and this session has
        # autoflush off — without this, the second call's own existence
        # check wouldn't see the first (still-pending) insert either, and
        # would try to insert a second time instead of updating.
        await session.flush()
        await repo.upsert_category(
            Category(slug="update-cat-explicit", name="Renamed", sort_order=2, is_active=False)
        )
        await session.flush()
        row = await session.get(CategoryRow, "update-cat-explicit")
        assert row.name == "Renamed"
        assert row.sort_order == 2
        assert row.is_active is False
