"""AAD-SEC-010 — `upsert_address` is now atomic (INSERT ... ON CONFLICT DO
UPDATE) and caps a user at MAX_ADDRESSES_PER_USER distinct labels.

Before this fix, a concurrent save of the *same* label could raise an
unhandled `IntegrityError` (a 500 to the customer) despite the unique
constraint already preventing duplicate rows, and there was no cap at all
on distinct labels — a script could create millions of address rows on one
account.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.errors import Conflict
from app.db.models import Address as AddressRow
from app.repositories.users import MAX_ADDRESSES_PER_USER, UserRepository


def _address(label: str, line1: str = "12 Farm Road") -> dict:
    return {
        "label": label, "line1": line1, "line2": "", "landmark": "",
        "city": "Hyderabad", "pincode": "500001", "latitude": None, "longitude": None,
    }


@pytest.fixture
def users(session) -> UserRepository:
    return UserRepository(session)


async def _make_user(users, session):
    user = await users.get_or_create_by_google(
        google_sub="addr_test_sub", email="addr@example.com", name="Addr Test",
    )
    await session.flush()
    return user


async def test_saving_the_same_label_twice_updates_it_in_place(users, session):
    user = await _make_user(users, session)

    await users.upsert_address(user["id"], _address("Home", line1="Old address"))
    await users.upsert_address(user["id"], _address("Home", line1="New address"))

    reread = await users.get_by_id(user["id"])
    assert len(reread["addresses"]) == 1
    assert reread["addresses"][0]["line1"] == "New address"


async def test_distinct_labels_are_capped(users, session):
    user = await _make_user(users, session)

    for i in range(MAX_ADDRESSES_PER_USER):
        await users.upsert_address(user["id"], _address(f"Label{i}"))

    with pytest.raises(Conflict):
        await users.upsert_address(user["id"], _address("OneTooMany"))

    reread = await users.get_by_id(user["id"])
    assert len(reread["addresses"]) == MAX_ADDRESSES_PER_USER


async def test_updating_an_existing_label_at_the_cap_is_not_blocked(users, session):
    """The cap only guards new rows — re-saving a label the account already
    has must keep working even when the account is already at the limit."""
    user = await _make_user(users, session)
    for i in range(MAX_ADDRESSES_PER_USER):
        await users.upsert_address(user["id"], _address(f"Label{i}"))

    await users.upsert_address(user["id"], _address("Label0", line1="Updated at the cap"))

    reread = await users.get_by_id(user["id"])
    assert len(reread["addresses"]) == MAX_ADDRESSES_PER_USER
    updated = next(a for a in reread["addresses"] if a["line1"] == "Updated at the cap")
    assert updated is not None


async def test_concurrent_saves_of_the_same_label_no_longer_raise_integrityerror(engine, session):
    """The regression this fix closes: two connections racing to create the
    *same* label used to mean the loser hit an unhandled IntegrityError
    instead of the atomic upsert it should have been."""
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async with factory() as setup_session:
        user = await UserRepository(setup_session).get_or_create_by_google(
            google_sub="addr_race_sub", email="race@example.com", name="Race Test",
        )
        await setup_session.commit()

    async def _save(line1: str) -> None:
        async with factory() as own_session:
            await UserRepository(own_session).upsert_address(
                user["id"], _address("Home", line1=line1)
            )
            await own_session.commit()

    # Both should succeed — no IntegrityError, no other exception.
    await asyncio.gather(_save("First writer"), _save("Second writer"))

    async with factory() as check_session:
        count = (
            await check_session.execute(
                select(func.count())
                .select_from(AddressRow)
                .where(AddressRow.user_id == user["id"])
            )
        ).scalar_one()
        assert count == 1, "one label must still mean one row, even after a race"
