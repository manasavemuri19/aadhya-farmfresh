"""OTP verification, including the idempotent-replay behaviour.

The scenario under test in `test_replaying_the_same_correct_code_within_grace_window_succeeds`
is a real bug this suite caught: a client that times out waiting for the verify
response — after the server already committed success — used to see the retry
fail with "invalid code", even though the account had genuinely just signed in.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import settings
from app.repositories.otp import OtpRepository


@pytest.fixture
def otps(session) -> OtpRepository:
    return OtpRepository(session)


PHONE = "+919812345670"


async def test_correct_code_succeeds(otps, session):
    await otps.create(PHONE, "123456")
    await session.flush()
    ok, reason = await otps.verify_and_consume(PHONE, "123456")
    assert ok is True
    assert reason == "ok"


async def test_wrong_code_fails_and_counts_as_an_attempt(otps, session):
    await otps.create(PHONE, "123456")
    await session.flush()
    ok, reason = await otps.verify_and_consume(PHONE, "000000")
    assert ok is False
    assert reason == "mismatch"


async def test_replaying_the_same_correct_code_within_grace_window_succeeds(otps, session):
    """The core fix: a client retry with the exact code that already worked
    must succeed again, not come back as an expired/invalid challenge."""
    await otps.create(PHONE, "123456")
    await session.flush()

    first = await otps.verify_and_consume(PHONE, "123456")
    assert first == (True, "ok")

    # Simulates the client never receiving that first success — e.g. its
    # connection dropped — and retrying with the identical code.
    second = await otps.verify_and_consume(PHONE, "123456")
    assert second == (True, "ok")

    third = await otps.verify_and_consume(PHONE, "123456")
    assert third == (True, "ok")


async def test_a_different_code_after_consumption_still_fails(otps, session):
    """Grace covers retrying the SAME code — it must not become a general
    amnesty for guessing after one correct hit."""
    await otps.create(PHONE, "123456")
    await session.flush()

    await otps.verify_and_consume(PHONE, "123456")
    ok, reason = await otps.verify_and_consume(PHONE, "999999")
    assert ok is False
    assert reason == "no_challenge"


async def test_replay_after_grace_window_closes_fails(otps, session):
    await otps.create(PHONE, "123456")
    await session.flush()
    await otps.verify_and_consume(PHONE, "123456")

    # Move the consumption timestamp outside the grace window directly,
    # rather than sleeping in a test.
    challenge = await otps._latest(PHONE)
    challenge.consumed_at = datetime.now(UTC) - timedelta(
        seconds=settings.otp_consumed_grace_seconds + 5
    )
    await session.flush()

    ok, reason = await otps.verify_and_consume(PHONE, "123456")
    assert ok is False
    assert reason == "no_challenge"


async def test_requesting_a_new_code_invalidates_a_consumed_one(otps, session):
    await otps.create(PHONE, "123456")
    await session.flush()
    await otps.verify_and_consume(PHONE, "123456")

    await otps.create(PHONE, "654321")
    await session.flush()

    # The old, already-used code must not still work after a fresh request.
    ok, reason = await otps.verify_and_consume(PHONE, "123456")
    assert ok is False

    ok, reason = await otps.verify_and_consume(PHONE, "654321")
    assert ok is True


async def test_expired_code_fails(otps, session):
    await otps.create(PHONE, "123456")
    await session.flush()
    challenge = await otps._latest(PHONE)
    challenge.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.flush()

    ok, reason = await otps.verify_and_consume(PHONE, "123456")
    assert ok is False
    assert reason == "expired"


async def test_too_many_wrong_attempts_locks_out(otps, session):
    await otps.create(PHONE, "123456")
    await session.flush()
    for _ in range(settings.otp_max_attempts):
        await otps.verify_and_consume(PHONE, "000000")

    ok, reason = await otps.verify_and_consume(PHONE, "123456")
    assert ok is False
    assert reason == "too_many_attempts"
