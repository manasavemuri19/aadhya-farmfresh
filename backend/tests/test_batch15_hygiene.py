"""Batch 15: Indian digit grouping, last_login_at on every sign-in, the
delivery-agent push fan-out's active-only filter, and the new
support_tickets -> users foreign key.
"""

from __future__ import annotations

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from app.db.models import SupportTicket as SupportTicketRow
from app.db.models import User as UserRow
from app.repositories.users import UserRepository
from app.schemas.common import Money, _indian_grouped

# --- AAD-QUAL-008: Indian digit grouping -----------------------------------


@pytest.mark.parametrize(
    "paise, expected",
    [
        (0, "0.00"),
        (50, "0.50"),
        (99999, "999.99"),
        (100000, "1,000.00"),
        (12345600, "1,23,456.00"),
        (123456700, "12,34,567.00"),
        (1234567800, "1,23,45,678.00"),
    ],
)
def test_money_rupees_display_uses_indian_grouping(paise, expected):
    assert Money(paise=paise).rupees_display == expected


def test_indian_grouped_matches_western_grouping_under_one_thousand():
    # Below 1,000 Indian and Western grouping produce the same string —
    # no comma either way.
    for n in (0, 1, 42, 999):
        assert _indian_grouped(n) == str(n)


# --- AAD-DATA-002: last_login_at on every sign-in, not just the first ------


async def test_last_login_at_advances_on_a_returning_users_second_sign_in(session):
    repo = UserRepository(session)
    first = await repo.get_or_create_by_google(
        google_sub="returning_user_sub", email="r@example.com", name="Returning",
    )
    first_login = (
        await session.execute(
            select(UserRow.last_login_at).where(UserRow.id == first["id"])
        )
    ).scalar_one()

    # Force enough of a gap that "advances" isn't a timing coincidence.
    await session.execute(
        update(UserRow)
        .where(UserRow.id == first["id"])
        .values(last_login_at=first_login.replace(year=first_login.year - 1))
    )
    await session.flush()

    await repo.get_or_create_by_google(
        google_sub="returning_user_sub", email="r@example.com", name="Returning",
    )
    second_login = (
        await session.execute(
            select(UserRow.last_login_at).where(UserRow.id == first["id"])
        )
    ).scalar_one()

    assert second_login > first_login.replace(year=first_login.year - 1)


async def test_get_or_create_by_google_still_returns_the_same_user_on_retry(session):
    repo = UserRepository(session)
    a = await repo.get_or_create_by_google(
        google_sub="stable_sub", email="s@example.com", name="Stable",
    )
    b = await repo.get_or_create_by_google(
        google_sub="stable_sub", email="s@example.com", name="Stable",
    )
    assert a["id"] == b["id"]


# --- AAD-PERF-004: active-only filter on the push fan-out query -----------


async def test_list_delivery_agent_ids_excludes_a_suspended_agent(session):
    repo = UserRepository(session)
    active = await repo.get_or_create_by_google(
        google_sub="agent_active", email="a1@example.com", name="Active Agent",
    )
    suspended = await repo.get_or_create_by_google(
        google_sub="agent_suspended", email="a2@example.com", name="Suspended Agent",
    )
    await session.execute(
        update(UserRow).where(UserRow.id == active["id"]).values(role="delivery_agent")
    )
    await session.execute(
        update(UserRow)
        .where(UserRow.id == suspended["id"])
        .values(role="delivery_agent", status="suspended")
    )
    await session.flush()

    ids = await repo.list_delivery_agent_ids()

    assert active["id"] in ids
    assert suspended["id"] not in ids


async def test_list_delivery_agent_ids_excludes_non_agents(session):
    repo = UserRepository(session)
    customer = await repo.get_or_create_by_google(
        google_sub="plain_customer", email="c@example.com", name="Customer",
    )
    ids = await repo.list_delivery_agent_ids()
    assert customer["id"] not in ids


# --- AAD-SEC-034: support_tickets.user_id -> users.id --------------------


async def test_support_ticket_cannot_reference_a_nonexistent_user(session):
    with pytest.raises(IntegrityError):
        await session.execute(
            insert(SupportTicketRow).values(
                id="tick_ghost",
                user_id="usr_does_not_exist",
                message="hello?",
                status="open",
            )
        )
        await session.flush()


async def test_deleting_a_user_cascades_to_their_support_tickets(session, user):
    await session.execute(
        insert(SupportTicketRow).values(
            id="tick_cascade",
            user_id=user["id"],
            message="still stuck",
            status="open",
        )
    )
    await session.flush()

    await session.execute(delete_user(user["id"]))
    await session.flush()

    remaining = (
        await session.execute(
            select(SupportTicketRow).where(SupportTicketRow.id == "tick_cascade")
        )
    ).scalars().first()
    assert remaining is None


def delete_user(user_id: str):
    from sqlalchemy import delete

    return delete(UserRow).where(UserRow.id == user_id)
