"""AAD-SEC-027: in-app, phone-number-free proof-of-delivery.

A 4-digit code is generated the moment an order becomes out_for_delivery,
shown to the customer in their own (already-authenticated) app, and entered
by the delivery agent to move the order to delivered. What's worth locking
in: the code is never trusted in the clear at verification time (only its
Argon2 hash is), a wrong guess costs an attempt rather than failing open or
locked forever, the code never outlives the delivery window (whichever way
the order leaves out_for_delivery), and an agent can no longer self-report
delivery with no evidence at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from app.core.errors import Conflict, Forbidden
from app.db.models import Order as OrderRow
from app.db.models import User as UserRow
from app.domain.enums import OrderStatus, PaymentMethod
from app.repositories.delivery import DeliveryRepository
from app.repositories.users import UserRepository
from app.schemas.auth import Address
from app.schemas.order import CartLineInput, CreateOrderRequest
from app.services.delivery_service import DeliveryService
from app.services.order_service import DELIVERY_OTP_MAX_ATTEMPTS

DEFAULT_ADDRESS = Address(
    label="Home", line1="12-3-45 Banjara Hills", city="Hyderabad", pincode="500034",
)


def order_request(lines, *, address: Address = DEFAULT_ADDRESS, **kw) -> CreateOrderRequest:
    return CreateOrderRequest(
        lines=[CartLineInput(sku=s, qty=q) for s, q in lines],
        address=address,
        **kw,
    )


@pytest.fixture
async def agent(session):
    """Matches test_delivery.py's own fixture — a second account promoted to
    delivery_agent directly, the way this role is actually assigned."""
    record = await UserRepository(session).get_or_create_by_google(
        google_sub="otp_agent_sub_0001", email="otp-agent@example.com", name="OTP Agent",
    )
    await session.execute(
        update(UserRow).where(UserRow.id == record["id"]).values(role="delivery_agent")
    )
    await session.flush()
    record["role"] = "delivery_agent"
    return record


@pytest.fixture
async def other_agent(session):
    """A second agent, for the ownership-check test — must never be able to
    verify a delivery code for an order accepted by someone else."""
    record = await UserRepository(session).get_or_create_by_google(
        google_sub="otp_agent_sub_0002", email="otp-agent-2@example.com", name="OTP Agent Two",
    )
    await session.execute(
        update(UserRow).where(UserRow.id == record["id"]).values(role="delivery_agent")
    )
    await session.flush()
    record["role"] = "delivery_agent"
    return record


@pytest.fixture
def delivery_repo(session) -> DeliveryRepository:
    return DeliveryRepository(session)


@pytest.fixture
def users_repo(session) -> UserRepository:
    return UserRepository(session)


@pytest.fixture
def delivery_service(delivery_repo, users_repo, order_service) -> DeliveryService:
    return DeliveryService(delivery_repo, users_repo, order_service)


async def _advance_to_out_for_delivery(delivery_service, order_id, agent_id) -> None:
    await delivery_service.accept(order_id, agent_id)
    await delivery_service.update_status(order_id, agent_id, OrderStatus.PACKED)
    await delivery_service.update_status(order_id, agent_id, OrderStatus.OUT_FOR_DELIVERY)


async def _row(session, order_id) -> OrderRow:
    await session.flush()
    return await session.get(OrderRow, order_id)


# ---------- code generation on dispatch ----------


async def test_out_for_delivery_generates_a_hashed_code_with_expiry_and_zero_attempts(
    order_service, delivery_service, agent, user, milk, session
):
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="otp-test-1",
    )
    await _advance_to_out_for_delivery(delivery_service, order.id, agent["id"])

    row = await _row(session, order.id)
    assert row.delivery_otp_hash is not None
    assert row.delivery_otp_hash.startswith("$argon2")
    assert row.delivery_otp_plain is not None
    assert len(row.delivery_otp_plain) == 4 and row.delivery_otp_plain.isdigit()
    # The hash is never just the plaintext code, or some trivial transform
    # of it — this is the whole point of storing both.
    assert row.delivery_otp_plain not in row.delivery_otp_hash
    assert row.delivery_otp_expires_at is not None
    assert row.delivery_otp_expires_at > datetime.now(UTC)
    assert row.delivery_otp_attempts == 0


async def test_delivery_code_is_not_shown_before_out_for_delivery(
    order_service, delivery_service, agent, user, milk
):
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="otp-test-2",
    )
    await delivery_service.accept(order.id, agent["id"])
    await delivery_service.update_status(order.id, agent["id"], OrderStatus.PACKED)

    view = await order_service.get_for_user(order.id, user["id"])
    assert view.delivery_code is None


async def test_delivery_code_is_shown_to_the_customer_while_out_for_delivery(
    order_service, delivery_service, agent, user, milk
):
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="otp-test-3",
    )
    await _advance_to_out_for_delivery(delivery_service, order.id, agent["id"])

    view = await order_service.get_for_user(order.id, user["id"])
    assert view.delivery_code is not None
    assert len(view.delivery_code) == 4


async def test_delivery_agent_view_never_carries_the_code(
    order_service, delivery_service, agent, user, milk
):
    """DeliveryOrderView is a structurally separate, narrower schema — this
    pins that the field simply doesn't exist on it, so an agent's ongoing
    list or status-update response can never leak it."""
    from app.schemas.delivery import DeliveryOrderView

    assert "delivery_code" not in DeliveryOrderView.model_fields

    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="otp-test-4",
    )
    accepted = await delivery_service.accept(order.id, agent["id"])
    await delivery_service.update_status(order.id, agent["id"], OrderStatus.PACKED)
    on_the_way = await delivery_service.update_status(
        order.id, agent["id"], OrderStatus.OUT_FOR_DELIVERY
    )
    assert not hasattr(accepted, "delivery_code")
    assert not hasattr(on_the_way, "delivery_code")


# ---------- verification ----------


async def test_correct_code_marks_the_order_delivered_and_clears_the_otp(
    order_service, delivery_service, agent, user, milk, session
):
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="otp-test-5",
    )
    await _advance_to_out_for_delivery(delivery_service, order.id, agent["id"])
    code = (await order_service.get_for_user(order.id, user["id"])).delivery_code

    delivered = await delivery_service.verify_delivery(order.id, agent["id"], code)
    assert delivered.status == OrderStatus.DELIVERED.value

    row = await _row(session, order.id)
    assert row.delivery_otp_hash is None
    assert row.delivery_otp_plain is None
    assert row.delivery_otp_expires_at is None
    assert row.delivery_otp_attempts == 0


async def test_wrong_code_is_rejected_and_counts_as_an_attempt(
    order_service, delivery_service, agent, user, milk, session
):
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="otp-test-6",
    )
    await _advance_to_out_for_delivery(delivery_service, order.id, agent["id"])
    code = (await order_service.get_for_user(order.id, user["id"])).delivery_code
    wrong = "0000" if code != "0000" else "1111"

    with pytest.raises(Conflict):
        await delivery_service.verify_delivery(order.id, agent["id"], wrong)

    row = await _row(session, order.id)
    assert row.delivery_otp_attempts == 1
    assert row.status == OrderStatus.OUT_FOR_DELIVERY.value  # unchanged

    # The real code still works afterwards — a wrong guess costs an
    # attempt, it doesn't burn the correct code.
    delivered = await delivery_service.verify_delivery(order.id, agent["id"], code)
    assert delivered.status == OrderStatus.DELIVERED.value


async def test_too_many_wrong_attempts_locks_out_even_a_correct_code(
    order_service, delivery_service, agent, user, milk, session
):
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="otp-test-7",
    )
    await _advance_to_out_for_delivery(delivery_service, order.id, agent["id"])
    code = (await order_service.get_for_user(order.id, user["id"])).delivery_code
    wrong = "0000" if code != "0000" else "1111"

    for _ in range(DELIVERY_OTP_MAX_ATTEMPTS):
        with pytest.raises(Conflict):
            await delivery_service.verify_delivery(order.id, agent["id"], wrong)

    # Locked out now — even the correct code is refused without staff
    # reissuing it (a fresh out_for_delivery transition, or a direct
    # staff override to DELIVERED).
    with pytest.raises(Conflict):
        await delivery_service.verify_delivery(order.id, agent["id"], code)


async def test_verify_delivery_rejects_an_order_that_is_not_out_for_delivery(
    order_service, delivery_service, agent, user, milk
):
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="otp-test-8",
    )
    await delivery_service.accept(order.id, agent["id"])
    # Still only PACKED — never went out for delivery, so no code exists.

    with pytest.raises(Conflict):
        await delivery_service.verify_delivery(order.id, agent["id"], "1234")


async def test_expired_code_is_rejected(
    order_service, delivery_service, agent, user, milk, session
):
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="otp-test-9",
    )
    await _advance_to_out_for_delivery(delivery_service, order.id, agent["id"])
    code = (await order_service.get_for_user(order.id, user["id"])).delivery_code

    await session.execute(
        update(OrderRow)
        .where(OrderRow.id == order.id)
        .values(delivery_otp_expires_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    await session.flush()

    with pytest.raises(Conflict):
        await delivery_service.verify_delivery(order.id, agent["id"], code)

    # And the customer's own view stops showing an expired code too.
    view = await order_service.get_for_user(order.id, user["id"])
    assert view.delivery_code is None


async def test_verify_delivery_checks_agent_ownership(
    order_service, delivery_service, agent, other_agent, user, milk
):
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="otp-test-10",
    )
    await _advance_to_out_for_delivery(delivery_service, order.id, agent["id"])
    code = (await order_service.get_for_user(order.id, user["id"])).delivery_code

    with pytest.raises(Forbidden):
        await delivery_service.verify_delivery(order.id, other_agent["id"], code)


# ---------- terminal-state hygiene ----------


async def test_cancelling_an_out_for_delivery_order_clears_the_otp(
    order_service, delivery_service, agent, user, milk, session
):
    """OUT_FOR_DELIVERY can route straight into cancelled/refunded without
    ever passing through DELIVERED — the code must not outlive the order."""
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="otp-test-11",
    )
    await _advance_to_out_for_delivery(delivery_service, order.id, agent["id"])

    await order_service.update_status(
        order_id=order.id, new_status=OrderStatus.CANCELLED, note="lost", actor="staff_1"
    )

    row = await _row(session, order.id)
    assert row.delivery_otp_hash is None
    assert row.delivery_otp_plain is None
    assert row.delivery_otp_expires_at is None


# ---------- the agent can no longer self-report delivery ----------


async def test_agent_can_no_longer_self_report_delivered_without_a_code(
    order_service, delivery_service, agent, user, milk
):
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="otp-test-12",
    )
    await _advance_to_out_for_delivery(delivery_service, order.id, agent["id"])

    with pytest.raises(Forbidden):
        await delivery_service.update_status(order.id, agent["id"], OrderStatus.DELIVERED)


async def test_staff_can_still_override_directly_to_delivered(
    order_service, delivery_service, agent, user, milk, session
):
    """The staff/admin path through /admin/orders/{id}/status is unchanged
    and deliberately still allowed to bypass the code entirely (a lost
    phone, a customer who never opens the app) — and still clears the OTP
    columns the same way the agent-verified path does."""
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="otp-test-13",
    )
    await _advance_to_out_for_delivery(delivery_service, order.id, agent["id"])

    view = await order_service.update_status(
        order_id=order.id, new_status=OrderStatus.DELIVERED, note="staff override", actor="staff_1"
    )
    assert view.status == OrderStatus.DELIVERED

    row = await _row(session, order.id)
    assert row.delivery_otp_hash is None
    assert row.delivery_otp_plain is None


# ---------- repository ----------


async def test_record_delivery_otp_attempt_increments_and_returns_new_count(orders, session):
    from app.core.ids import new_id
    from app.repositories.users import UserRepository

    buyer = await UserRepository(session).get_or_create_by_google(
        google_sub="otp_repo_buyer", email="otp-repo-buyer@example.com", name="Repo Buyer",
    )
    await session.flush()
    order_id = new_id("ord")
    await session.execute(
        OrderRow.__table__.insert().values(
            id=order_id,
            order_number="OTP-REPO-1",
            user_id=buyer["id"],
            status=OrderStatus.OUT_FOR_DELIVERY.value,
            subtotal_paise=1000,
            delivery_fee_paise=0,
            total_paise=1000,
            address=DEFAULT_ADDRESS.model_dump(mode="json"),
            delivery_otp_attempts=0,
        )
    )
    await session.flush()

    first = await orders.record_delivery_otp_attempt(order_id)
    second = await orders.record_delivery_otp_attempt(order_id)
    assert (first, second) == (1, 2)

    missing = await orders.record_delivery_otp_attempt("ord_does_not_exist")
    assert missing is None
