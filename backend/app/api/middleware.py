"""Cross-cutting HTTP concerns."""

from __future__ import annotations

import logging
import re
import time
import uuid

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.v1.router import API_V1_PREFIX
from app.core.logging import request_id_var

log = logging.getLogger("app.access")

REQUEST_ID_HEADER = "X-Request-Id"

# AAD-OPS-004: this used to be the literal strings "/health" and
# "/health/live" — the paths those routes have *before* being mounted.
# `app.main` mounts them under API_V1_PREFIX, so the real, resolved paths
# are "/v1/health" and "/v1/health/live", and the set here never matched
# anything. Importing the same prefix constant `main.py` uses means the two
# cannot drift apart again the way they already had.
_UNLOGGED_PATHS = {f"{API_V1_PREFIX}/health", f"{API_V1_PREFIX}/health/live"}

# AAD-SEC-006: whatever the client sends used to become the request id
# verbatim — no length cap, no charset restriction — and was then written
# into every log line for that request and echoed back in a response
# header. An 8 MB header value written to the log stream once per line is a
# volume attack; a value containing control characters is a log-injection
# attempt. Accept the inbound id only if it already looks like one of ours;
# otherwise mint a fresh one, exactly as if none had been sent.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a request id, log the outcome, and echo the id back.

    Honours an inbound X-Request-Id so a trace started on the mobile client
    stays joined up across the whole call — but only when it is well-formed;
    see AAD-SEC-006 above.
    """

    async def dispatch(self, request: Request, call_next):
        inbound = request.headers.get(REQUEST_ID_HEADER)
        request_id = inbound if inbound and _REQUEST_ID_RE.match(inbound) else uuid.uuid4().hex[:16]
        token = request_id_var.set(request_id)
        request.state.request_id = request_id
        started = time.perf_counter()

        # AAD-OPS-003: the `reset` used to sit in its own `finally` around
        # only `call_next`, so it ran — clearing the contextvar back to the
        # "-" default — *before* the success log line below, which is what
        # `JsonFormatter` reads at format time. Every successful request's
        # access-log line recorded request_id: "-"; only the exception path
        # kept its id, because that log call sits inside its own `except`,
        # ahead of the reset. The whole dispatch body is now inside one
        # `try`, and the reset is the outer `finally` around all of it, so
        # both log calls execute while the contextvar is still set.
        try:
            try:
                response: Response = await call_next(request)
            except Exception:
                duration_ms = round((time.perf_counter() - started) * 1000, 2)
                log.exception(
                    "request failed",
                    extra={
                        "method": request.method,
                        "path": request.url.path,
                        "duration_ms": duration_ms,
                    },
                )
                raise

            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            response.headers[REQUEST_ID_HEADER] = request_id
            if request.url.path not in _UNLOGGED_PATHS:
                log.info(
                    "request",
                    extra={
                        "method": request.method,
                        "path": request.url.path,
                        "status": response.status_code,
                        "duration_ms": duration_ms,
                    },
                )
            return response
        finally:
            request_id_var.reset(token)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Baseline headers. The API serves JSON only, so the policy is restrictive."""

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault(
            "Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"
        )
        return response


# AAD-SEC-013: nothing capped request body size. FastAPI/Starlette buffer the
# full body into memory before any route or dependency runs, so a single
# request with an enormous body is read entirely into memory before your own
# code gets a chance to reject it — a handful of concurrent ones OOM the
# container. `BaseHTTPMiddleware` cannot fix this: by the time its
# `dispatch()` runs, Starlette has already started consuming the body
# through its own internal receive channel. This wraps the raw ASGI
# `receive` callable one layer below that, counting bytes as they arrive and
# cutting the connection short with a 413 the moment the limit is crossed —
# before FastAPI's request parsing ever sees the rest of the body.
_DEFAULT_BODY_LIMIT_BYTES = 1 * 1024 * 1024  # 1 MB — generous for this API's JSON bodies


class _BodyTooLargeError(Exception):
    pass


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, *, max_bytes: int = _DEFAULT_BODY_LIMIT_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # A well-behaved client sends Content-Length — checked first as a
        # fast, cheap rejection. Nothing stops a client from lying about it
        # (or omitting it, as chunked transfer encoding does), so the actual
        # byte count is still enforced below as the body streams in either way.
        headers = dict(scope.get("headers") or [])
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                if int(declared) > self.max_bytes:
                    await _reject_too_large(send)
                    return
            except ValueError:
                pass  # malformed header — let the real byte count below decide

        seen = 0

        async def limited_receive():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b"") or b"")
                if seen > self.max_bytes:
                    raise _BodyTooLargeError()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLargeError:
            await _reject_too_large(send)


async def _reject_too_large(send: Send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": (
                b'{"error":{"code":"payload_too_large",'
                b'"message":"Request body is too large."}}'
            ),
        }
    )
