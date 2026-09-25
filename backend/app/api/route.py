"""The request's transaction boundary.

AAD-REL-001: the old `db_session` dependency committed in the code *after*
`yield`, which FastAPI runs during dependency teardown — after the route's
Response object has already been built. A commit failure there has nowhere
to go: by the time it's raised, the success response may already be on its
way to the client (verified with a real socket in
tests/test_transaction_boundary.py; an in-process ASGI transport does not
show this, because it doesn't model bytes the OS has already flushed to a
real connection).

`TransactionalRoute` moves the commit to run *around* the whole endpoint
call instead — including FastAPI's own dependency teardown — so a commit
failure is just a normal exception raised before anything is handed off to
whatever actually sends bytes to the client. That's what lets it become a
5xx instead of a phantom 200.

Every router that touches the database uses `route_class=TransactionalRoute`
(see each routes/*.py file). `db_session` (api/deps.py) now only creates the
session and stashes it on `request.state.db_session` for this class to find;
it no longer commits, rolls back, or closes it — that lifecycle is owned
here, in one place, so it can't drift back out of sync with where the
response actually gets sent.
"""

from __future__ import annotations

import logging

from fastapi import Request, Response
from fastapi.routing import APIRoute

from app.core import outbox

log = logging.getLogger(__name__)


class TransactionalRoute(APIRoute):
    def get_route_handler(self):
        original_route_handler = super().get_route_handler()

        async def transactional_handler(request: Request) -> Response:
            # AAD-REL-004: opens this request's after-commit batch. Any
            # `defer_until_commit` call anywhere below — currently only
            # OrderService's push notifications — queues here instead of
            # running immediately; `outbox.drain()` below only runs it once
            # `session.commit()` has actually succeeded, and never at all
            # on any path that doesn't reach it.
            outbox_token = outbox.start_batch()
            try:
                # Runs the endpoint plus every dependency's own teardown
                # (including a plain `yield`-and-return db_session) end to
                # end. Anything the route raises — a business AppError, a
                # validation error, an unhandled bug — propagates out of
                # this `await` unchanged; it hasn't been converted into a
                # Response yet at this point, because the exception-handling
                # middleware that does that conversion sits *outside*
                # routing, not inside it.
                try:
                    response = await original_route_handler(request)
                except Exception:
                    # AAD-REL-007: found while writing AAD-OPS-020's first
                    # real HTTP-layer tests — a route test on
                    # `GET /orders/{id}` for a missing order (a plain,
                    # everyday 404) left the test engine's connection pool
                    # one connection short every time it ran, confirmed
                    # directly against `engine.pool.checkedout()`. Before
                    # this fix, this function's only `try/finally` that
                    # owned the session's rollback/close sat entirely below
                    # this `await` — so any exception raised while building
                    # the response (a business AppError like this 404, a
                    # 422 validation error, an unhandled 500) skipped that
                    # block completely. The session was never closed: not
                    # rolled back, not returned to the pool, just
                    # permanently checked out. `db_session` (api/deps.py)
                    # deliberately does nothing after its own `yield` —
                    # this class is where that lifecycle is supposed to
                    # live — so nothing else in the dependency-teardown
                    # chain was ever going to catch this either. That makes
                    # it worse than `AAD-REL-001`, not milder: 401s, 404s
                    # and 422s are the *common* responses an API returns,
                    # so this leaked a connection on a large fraction of
                    # all traffic, and would exhaust the pool far sooner
                    # than AAD-REL-001's exact race ever would. No
                    # error-level log here — an ordinary 404 or validation
                    # error is routine traffic, not something to log as an
                    # exception; `handle_unexpected` already logs the
                    # genuinely-unexpected-bug case once the exception
                    # reaches it.
                    session = getattr(request.state, "db_session", None)
                    if session is not None:
                        await session.rollback()
                        await session.close()
                    raise

                session = getattr(request.state, "db_session", None)
                if session is None:
                    # No db_session dependency was ever resolved for this
                    # route (e.g. /health/live) — nothing to commit, and
                    # nothing queued in this batch either.
                    return response

                try:
                    await session.commit()
                except Exception:
                    # The route thought it succeeded and already built a
                    # 2xx response — but the write never actually landed.
                    # Roll back and re-raise so the exception-handling
                    # middleware converts this into a 500 instead of the
                    # stale success response ever being returned. The
                    # queued batch (if any) is deliberately left undrained
                    # on this path — see AAD-REL-004 above: nothing in it
                    # may fire for a write that didn't happen.
                    log.exception(
                        "commit failed after a successful response was already built",
                        extra={"path": request.url.path},
                    )
                    await session.rollback()
                    raise
                else:
                    # AAD-REL-004: drains only on the success path, and
                    # deliberately BEFORE closing the session below.
                    # OrderService's deferred push notifications resolve
                    # their recipients' tokens (PushTokenRepository) using
                    # this same request-scoped session; running them after
                    # session.close() would let that query implicitly open
                    # a fresh, never-cleaned-up transaction on the reused
                    # session, which leaks until Postgres's
                    # idle_in_transaction_session_timeout kills it — this
                    # is exactly what caused a real 15-second stall between
                    # sequential requests in tests/test_payment_endpoints.py
                    # (the only test file that runs the real app lifespan,
                    # and so the only one where a leaked idle transaction on
                    # a reused connection actually blocked the next test's
                    # startup long enough to notice). Mirrors the identical
                    # try/except/else/finally shape in db/base.py's
                    # `session_scope`, the sweeper's own commit boundary.
                    await outbox.drain()
                finally:
                    await session.close()

                return response
            finally:
                outbox.end_batch(outbox_token)

        return transactional_handler
