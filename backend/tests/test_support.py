"""Support tickets — the mailbox behind Help & Support's "still stuck?"
fallback. Thin feature, thin tests: create, and the free-form context
field round-trips.
"""

from __future__ import annotations

import pytest

from app.repositories.support import SupportRepository
from app.services.support_service import SupportService


@pytest.fixture
def support(session) -> SupportRepository:
    return SupportRepository(session)


@pytest.fixture
def support_service(support) -> SupportService:
    return SupportService(support)


async def test_create_ticket_round_trips(support, session, user):
    ticket = await support.create(
        user_id=user.id, message="My order has been stuck on Packed for a day.",
        context_node_id="orders_status",
    )
    await session.flush()

    assert ticket.id.startswith("sup_")
    assert ticket.user_id == user.id
    assert ticket.message == "My order has been stuck on Packed for a day."
    assert ticket.context_node_id == "orders_status"
    assert ticket.created_at is not None


async def test_context_node_id_is_optional(support, user):
    ticket = await support.create(
        user_id=user.id, message="Something else entirely.", context_node_id=None,
    )
    assert ticket.context_node_id is None


async def test_service_returns_only_id_and_created_at(support_service, user):
    """The mailbox is server-side-only beyond this — nothing about the
    ticket's content or context should round-trip back to the client."""
    result = await support_service.submit(
        user_id=user.id, message="Test", context_node_id="root",
    )
    assert result.id.startswith("sup_")
    assert result.created_at is not None
