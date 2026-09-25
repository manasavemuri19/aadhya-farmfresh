"""Cross-cutting HTTP concerns."""

from __future__ import annotations

import logging
import re
import time
import uuid

from fastapi import Request
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


class RequestContextMiddleware:
    """Assign a request id, log the outcome, and echo the id back.

    Honours an inbound X-Request-Id so a trace started on the mobile client
    stays joined up across the whole call — but only when it is well-formed;
    see AAD-SEC-006 above.

    AAD-PERF-005: rewritten from `BaseHTTPMiddleware` to pure ASGI, same
    reasoning `BodySizeLimitMiddleware` below already documented for itself —
    `BaseHTTPMiddleware` wraps every single request in an extra `anyio` task
    plus a pair of memory-object streams to bridge its `dispatch()` callback
    style back onto the real ASGI protocol underneath it. That's real
    per-request overhead (this middleware runs on *every* request, unlike
    `BodySizeLimitMiddleware` which only inspects bytes as they arrive), and
    it's also documented to interfere with server-sent-events / streaming
    responses and with Starlette's `BackgroundTask` (a background task
    attached to a response only starts once `BaseHTTPMiddleware`'s own
    wrapping task exits, not once the response is actually flushed to the
    client) — this app has neither today, but there's no reason to keep
    paying the cost of a pattern already known to cause problems for either.
    Behavior is unchanged: same request-id minting/validation, same
    contextvar, same `request.state.request_id` (Starlette's `Request.state`
    is just a thin wrapper around `scope["state"]`, so setting it there
    before calling the wrapped app is exactly equivalent to the old
    `request.state.request_id = request_id`), same access-log lines
    (including the unlogged-paths list), same response header, same
    reset-in-`finally`.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        inbound = request.headers.get(REQUEST_ID_HEADER)
        request_id = inbound if inbound and _REQUEST_ID_RE.match(inbound) else uuid.uuid4().hex[:16]
        token = request_id_var.set(request_id)
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.perf_counter()
        status_holder: dict[str, int | None] = {"status": None}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                headers = list(message.get("headers") or [])
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message["headers"] = headers
            await send(message)

        # AAD-OPS-003: the `reset` used to sit in its own `finally` around
        # only `call_next`, so it ran — clearing the contextvar back to the
        # "-" default — *before* the success log line below, which is what
        # `JsonFormatter` reads at format time. Every successful request's
        # access-log line recorded request_id: "-"; only the exception path
        # kept its id, because that log call sits inside its own `except`,
        # ahead of the reset. The whole body below is inside one `try`, and
        # the reset is the outer `finally` around all of it, so both log
        # calls execute while the contextvar is still set.
        try:
            try:
                await self.app(scope, receive, send_wrapper)
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
            if request.url.path not in _UNLOGGED_PATHS:
                log.info(
                    "request",
                    extra={
                        "method": request.method,
                        "path": request.url.path,
                        "status": status_holder["status"],
                        "duration_ms": duration_ms,
                    },
                )
        finally:
            request_id_var.reset(token)


class SecurityHeadersMiddleware:
    """Baseline headers. The API serves JSON only, so the policy is restrictive.

    AAD-PERF-005: rewritten from `BaseHTTPMiddleware` to pure ASGI — same
    reasoning as `RequestContextMiddleware` above. The old `setdefault`
    semantics (never overwrite a header a downstream handler already set
    explicitly) are preserved by checking the outgoing header names directly
    against what the response already carries, rather than relying on
    Starlette's `Response.headers.setdefault`.
    """

    # AAD-SEC-018: low practical value for a native mobile client (which
    # never falls back to plain HTTP the way a browser's first request can),
    # but it's the standard baseline header an audit expects, and harmless to
    # send unconditionally — a browser hitting this API directly (the
    # interactive docs at /docs, say) gets the same protection a normal web
    # app would.
    _BASELINE_HEADERS = (
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"referrer-policy", b"no-referrer"),
        (b"cache-control", b"no-store"),
        (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
        (b"strict-transport-security", b"max-age=31536000; includeSubDomains"),
    )

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                existing = {name.lower() for name, _ in (message.get("headers") or [])}
                headers = list(message.get("headers") or [])
                headers.extend(
                    (name, value)
                    for name, value in self._BASELINE_HEADERS
                    if name not in existing
                )
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


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
