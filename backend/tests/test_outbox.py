"""AAD-REL-004 — unit tests for the outbox mechanism itself
(`app/core/outbox.py`), independent of OrderService or any commit boundary:
a deferred effect must not run before `drain()`, must never run at all if
`drain()` is never called (the rollback case), one failing effect must not
stop the rest, and batches must not leak into each other.
"""

from __future__ import annotations

from app.core import outbox


async def test_defer_with_no_active_batch_runs_immediately():
    calls = []

    async def effect():
        calls.append(1)

    await outbox.defer_until_commit(effect)
    assert calls == [1]


async def test_deferred_effect_does_not_run_until_drain():
    token = outbox.start_batch()
    calls = []

    async def effect():
        calls.append(1)

    try:
        await outbox.defer_until_commit(effect)
        assert calls == [], "must not run before drain()"
        await outbox.drain()
        assert calls == [1]
    finally:
        outbox.end_batch(token)


async def test_deferred_effect_never_runs_if_drain_is_never_called():
    """The rollback case: a batch that opens, queues an effect, and closes
    without ever draining — exactly what both commit boundaries do when
    their transaction fails."""
    token = outbox.start_batch()
    calls = []

    async def effect():
        calls.append(1)

    try:
        await outbox.defer_until_commit(effect)
        assert calls == []
    finally:
        outbox.end_batch(token)

    assert calls == [], "a never-drained batch must never run its effects"


async def test_one_failing_effect_does_not_stop_the_rest_or_raise():
    token = outbox.start_batch()
    calls = []

    async def boom():
        raise RuntimeError("simulated failure in one deferred effect")

    async def ok():
        calls.append("ok")

    try:
        await outbox.defer_until_commit(boom)
        await outbox.defer_until_commit(ok)
        await outbox.drain()  # must not raise, despite boom() failing
    finally:
        outbox.end_batch(token)

    assert calls == ["ok"]


async def test_batches_do_not_leak_into_a_later_one():
    token1 = outbox.start_batch()
    calls = []

    async def effect():
        calls.append(1)

    await outbox.defer_until_commit(effect)
    outbox.end_batch(token1)  # ended without draining — simulates a rollback

    token2 = outbox.start_batch()
    try:
        await outbox.drain()  # nothing was queued in *this* batch
    finally:
        outbox.end_batch(token2)

    assert calls == [], "an earlier, undrained batch's effect must not surface in a later one"


async def test_drain_with_nothing_queued_is_a_safe_no_op():
    token = outbox.start_batch()
    try:
        await outbox.drain()  # nothing queued at all
    finally:
        outbox.end_batch(token)
