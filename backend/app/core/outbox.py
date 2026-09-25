"""Best-effort side effects that must never fire until the current
transaction has actually committed.

AAD-REL-004: `order_service.py`'s push notifications used to be awaited
inline, inside the same transaction the request's commit-owning boundary
(`TransactionalRoute` for HTTP requests, `session_scope()` for the
background sweeper) commits only *after* the route/task returns. The
section's own comment claimed otherwise ("every call site below is *after*
the database write already committed") — it wasn't; `OrderRepository.transition()`
ends in `session.flush()`, not `commit()`. Two consequences: a customer's
phone could say "Order confirmed" before the write actually committed, with
no way to un-send it if the commit then failed; and the push itself is an
outbound HTTP call made while still holding the order/inventory row locks
the transaction opened, the same problem `AAD-PAY-003` fixed for gateway
calls.

A `ContextVar` rather than a parameter threaded through every layer between
a commit boundary and `OrderService`'s push call sites, several calls deep
in business logic — the same shape already used for request-scoped state in
`core/logging.py` (`request_id_var`, `user_id_var`), for the same reason:
many unrelated call sites need it without every one of them accepting and
forwarding a new argument.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar

log = logging.getLogger(__name__)

Effect = Callable[[], Awaitable[None]]

_pending_var: ContextVar[list[Effect] | None] = ContextVar("pending_after_commit", default=None)


def start_batch() -> object:
    """Call once at the top of a commit-owning boundary. Returns a token for
    `end_batch`. Any `defer_until_commit` call made anywhere below this
    point, in the same task, queues onto this batch instead of running
    immediately."""
    return _pending_var.set([])


def end_batch(token: object) -> None:
    """Always call in a `finally` matching `start_batch`, regardless of
    whether `drain` ran — resets the ContextVar so a queue never leaks
    across requests sharing a task (it wouldn't, today; this is cheap
    insurance against ever depending on that)."""
    _pending_var.reset(token)  # type: ignore[arg-type]


async def defer_until_commit(effect: Effect) -> None:
    """Queue a best-effort side effect to run once the current commit
    boundary's transaction has actually committed.

    Falls back to awaiting the effect immediately when called with no
    active batch (a direct service-layer call in a script or test, outside
    both `TransactionalRoute` and `session_scope()`) — the closest
    equivalent to this function's pre-AAD-REL-004 behaviour, so a call site
    that isn't wrapped by either commit boundary doesn't silently lose its
    notification forever. Every real HTTP request and every sweeper pass
    goes through one or the other. `async def` (rather than scheduling a
    fire-and-forget task) so this fallback path still actually completes
    before the caller moves on, matching the old inline-await behaviour
    exactly rather than a background task that a short-lived script or test
    process could exit before it ever runs.
    """
    pending = _pending_var.get()
    if pending is None:
        log.debug("defer_until_commit called with no active batch; running inline")
        await effect()
        return
    pending.append(effect)


async def drain() -> None:
    """Call once, only after the commit boundary's `commit()` has actually
    succeeded — never on a rollback path. Runs every queued effect,
    independently: one effect raising (a bug in a call site, not the
    already-swallowed-and-logged failures `PushService` itself handles)
    never stops the rest from running, and never propagates out to the
    caller — a best-effort side effect must not turn an already-successful
    response into a 500 after the fact.
    """
    pending = _pending_var.get()
    if not pending:
        return
    for effect in pending:
        try:
            await effect()
        except Exception:
            log.exception("deferred after-commit effect failed")
    pending.clear()
