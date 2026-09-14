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

log = logging.getLogger(__name__)


class TransactionalRoute(APIRoute):
    def get_route_handler(self):
        original_route_handler = super().get_route_handler()

        async def transactional_handler(request: Request) -> Response:
            # Runs the endpoint plus every dependency's own teardown
            # (including a plain `yield`-and-return db_session) end to end.
            # Anything the route raises — a business AppError, a validation
            # error, an unhandled bug — propagates out of this `await`
            # unchanged; it hasn't been converted into a Response yet at
            # this point, because the exception-handling middleware that
            # does that conversion sits *outside* routing, not inside it.
            response = await original_route_handler(request)

            session = getattr(request.state, "db_session", None)
            if session is None:
                # No db_session dependency was ever resolved for this route
                # (e.g. /health/live) — nothing to commit.
                return response

            try:
                await session.commit()
            except Exception:
                # The route thought it succeeded and already built a 2xx
                # response — but the write never actually landed. Roll back
                # and re-raise so the exception-handling middleware converts
                # this into a 500 instead of the stale success response ever
                # being returned.
                log.exception(
                    "commit failed after a successful response was already built",
                    extra={"path": request.url.path},
                )
                await session.rollback()
                raise
            finally:
                await session.close()

            return response

        return transactional_handler
