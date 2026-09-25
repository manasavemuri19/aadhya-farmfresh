"""AAD-BIZ-005 — support tickets used to be write-only: `SupportRepository`
had exactly one method (`create`), no admin endpoint ever read the table,
and nothing notified staff when a ticket arrived. This file covers the
fix's three pieces: the repository's new `list`/`close`, the service's
deferred "new ticket" push to staff (mirroring OrderService's own
AAD-REL-004 wiring), and the admin HTTP routes (guard, pagination, close).
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api import deps
from app.core import outbox
from app.core.errors import RateLimited
from app.core.ids import new_id
from app.core.security import issue_access_token
from app.db.models import User as UserRow
from app.domain.enums import Role, SupportTicketStatus
from app.repositories.support import SupportRepository
from app.repositories.users import UserRepository
from app.services.support_service import SupportService

# tests/test_support.py defines the same fixture, but pytest fixtures aren't
# shared across files without a conftest.py entry — redefined here rather
# than moved, to keep this file's own dependencies self-contained.


@pytest.fixture
def support(session) -> SupportRepository:
    return SupportRepository(session)


# ---------- repository ----------


async def test_a_new_ticket_defaults_to_open(support, user):
    ticket = await support.create(user_id=user["id"], message="Help", context_node_id=None)
    assert ticket.status == SupportTicketStatus.OPEN.value


async def test_list_with_no_filter_returns_every_status(support, user):
    a = await support.create(user_id=user["id"], message="one", context_node_id=None)
    b = await support.create(user_id=user["id"], message="two", context_node_id=None)
    assert await support.close(a.id)

    page = await support.list(limit=10)
    ids = {t.id for t in page}
    assert {a.id, b.id} <= ids


async def test_list_filters_by_status(support, user):
    open_ticket = await support.create(user_id=user["id"], message="open one", context_node_id=None)
    closed_ticket = await support.create(
        user_id=user["id"], message="closed one", context_node_id=None
    )
    await support.close(closed_ticket.id)

    open_only = await support.list(status=SupportTicketStatus.OPEN, limit=10)
    open_ids = {t.id for t in open_only}
    assert open_ticket.id in open_ids
    assert closed_ticket.id not in open_ids

    closed_only = await support.list(status=SupportTicketStatus.CLOSED, limit=10)
    assert closed_ticket.id in {t.id for t in closed_only}


async def test_list_paginates_with_the_one_extra_row_trick(support, user):
    for i in range(3):
        await support.create(user_id=user["id"], message=f"ticket {i}", context_node_id=None)

    page = await support.list(limit=2)
    assert len(page) == 3, "list() itself returns limit+1 rows — the caller trims and flags"


async def test_close_succeeds_once_then_reports_false(support, user):
    ticket = await support.create(user_id=user["id"], message="Help", context_node_id=None)
    assert await support.close(ticket.id) is True
    assert await support.close(ticket.id) is False, "a double-close must not silently succeed twice"


async def test_close_of_an_unknown_ticket_reports_false(support):
    assert await support.close("sup_does_not_exist") is False


# ---------- service: deferred staff push ----------


class _FakePush:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str]] = []

    async def notify_users(self, user_ids, *, title, body, data=None) -> None:
        self.calls.append((list(user_ids), title))


async def _make_staff(session, sub: str) -> str:
    users = UserRepository(session)
    staff = await users.get_or_create_by_google(
        google_sub=sub, email=f"{sub}@example.com", name="Staff"
    )
    await session.execute(
        sa_update(UserRow).where(UserRow.id == staff["id"]).values(role=Role.STAFF.value)
    )
    await session.flush()
    return staff["id"]


async def test_submit_defers_the_staff_push_until_drain(session, support, user):
    staff_id = await _make_staff(session, "biz005_staff_1")
    push = _FakePush()
    svc = SupportService(support, users=UserRepository(session), push=push)

    token = outbox.start_batch()
    try:
        await svc.submit(user_id=user["id"], message="Help", context_node_id=None)
        assert push.calls == [], "must not have pushed before the batch drained"
        await outbox.drain()
        assert push.calls, "should have queued a staff notification"
        assert push.calls[0][0] == [staff_id]
    finally:
        outbox.end_batch(token)


async def test_submit_never_pushes_if_the_batch_is_never_drained(session, support, user):
    await _make_staff(session, "biz005_staff_2")
    push = _FakePush()
    svc = SupportService(support, users=UserRepository(session), push=push)

    token = outbox.start_batch()
    try:
        await svc.submit(user_id=user["id"], message="Help", context_node_id=None)
    finally:
        outbox.end_batch(token)  # deliberately never drained

    assert push.calls == [], "a push must never fire for a ticket whose transaction never committed"


async def test_submit_with_no_staff_accounts_is_a_quiet_no_op(session, support, user):
    """No staff exist yet — must not raise, must not queue anything."""
    push = _FakePush()
    svc = SupportService(support, users=UserRepository(session), push=push)

    token = outbox.start_batch()
    try:
        await svc.submit(user_id=user["id"], message="Help", context_node_id=None)
        await outbox.drain()
    finally:
        outbox.end_batch(token)

    assert push.calls == []


async def test_list_for_staff_maps_repository_rows_to_the_view_schema(session, support, user):
    ticket = await support.create(user_id=user["id"], message="Help me", context_node_id="root")
    svc = SupportService(support)

    page = await svc.list_for_staff(limit=10)
    match = next(t for t in page.items if t.id == ticket.id)
    assert match.user_id == user["id"]
    assert match.message == "Help me"
    assert match.context_node_id == "root"
    assert match.status == SupportTicketStatus.OPEN.value
    assert isinstance(match.created_at, datetime)


# ---------- HTTP layer: guard, pagination, close, rate limit ----------


@pytest.fixture
async def client(engine):
    from app.main import app as real_app

    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async def override_db_session(request: Request):
        session = factory()
        request.state.db_session = session
        yield session

    real_app.dependency_overrides[deps.db_session] = override_db_session
    transport = ASGITransport(app=real_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
    real_app.dependency_overrides.pop(deps.db_session, None)


@pytest.fixture
async def seeded_users(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    customer_id = new_id("usr", 12)
    staff_id = new_id("usr", 12)
    async with factory() as session:
        session.add(
            UserRow(
                id=customer_id, google_sub="biz005_http_cust", email="cust@example.com",
                name="Cust",
            )
        )
        session.add(
            UserRow(
                id=staff_id, google_sub="biz005_http_staff", email="staff@example.com",
                name="Staff", role=Role.STAFF.value,
            )
        )
        await session.commit()
    return customer_id, staff_id


async def test_a_plain_customer_cannot_list_tickets(client, seeded_users):
    customer_id, _staff_id = seeded_users
    token = issue_access_token(customer_id, role="customer")
    resp = await client.get(
        "/v1/admin/support/tickets", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 403


async def test_staff_can_submit_list_and_close_a_ticket_over_http(client, seeded_users):
    customer_id, staff_id = seeded_users
    customer_token = issue_access_token(customer_id, role="customer")
    staff_token = issue_access_token(staff_id, role="staff")

    created = await client.post(
        "/v1/support/tickets",
        json={"message": "My order never arrived", "context_node_id": "orders_status"},
        headers={"Authorization": f"Bearer {customer_token}"},
    )
    assert created.status_code == 201, created.text
    ticket_id = created.json()["id"]

    listed = await client.get(
        "/v1/admin/support/tickets", headers={"Authorization": f"Bearer {staff_token}"}
    )
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert any(item["id"] == ticket_id for item in body["items"])
    assert any(item["status"] == "open" for item in body["items"] if item["id"] == ticket_id)

    closed = await client.post(
        f"/v1/admin/support/tickets/{ticket_id}/close",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["status"] == "closed"

    closed_again = await client.post(
        f"/v1/admin/support/tickets/{ticket_id}/close",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert closed_again.status_code == 404, "closing an already-closed ticket must not silently 200"


async def test_status_filter_over_http_excludes_the_other_status(client, seeded_users):
    customer_id, staff_id = seeded_users
    customer_token = issue_access_token(customer_id, role="customer")
    staff_token = issue_access_token(staff_id, role="staff")

    resp = await client.post(
        "/v1/support/tickets", json={"message": "filter me"},
        headers={"Authorization": f"Bearer {customer_token}"},
    )
    ticket_id = resp.json()["id"]
    await client.post(
        f"/v1/admin/support/tickets/{ticket_id}/close",
        headers={"Authorization": f"Bearer {staff_token}"},
    )

    open_only = await client.get(
        "/v1/admin/support/tickets", params={"status": "open"},
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert ticket_id not in {item["id"] for item in open_only.json()["items"]}

    closed_only = await client.get(
        "/v1/admin/support/tickets", params={"status": "closed"},
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert ticket_id in {item["id"] for item in closed_only.json()["items"]}


# ---------- rate limit ----------


async def test_ticket_submission_is_rate_limited_per_user(session, support, user):
    """Direct RateLimiter check, not over HTTP — the limiter itself is a
    plain in-process fixed-window keyed by user id (app/core/rate_limit.py);
    this proves the specific window `routes/support.py` wires up for ticket
    submission (5/hour) actually trips, without needing six real HTTP round
    trips or a real clock."""
    from app.api.v1.routes.support import _ticket_per_user_per_hour

    key = f"rate-limit-test-{user['id']}"
    for _ in range(5):
        _ticket_per_user_per_hour.check(key)
    with pytest.raises(RateLimited):
        _ticket_per_user_per_hour.check(key)
