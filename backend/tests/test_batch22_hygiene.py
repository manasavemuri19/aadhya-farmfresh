"""Batch 22: AAD-PERF-005 — RequestContextMiddleware and
SecurityHeadersMiddleware rewritten from `starlette.middleware.base.
BaseHTTPMiddleware` to plain ASGI callables.

These are deliberately unit tests against the middleware classes directly,
with a minimal fake ASGI app underneath, rather than another pass through a
real HTTP client — `test_security_headers_and_hosts.py` and
`test_ops_hardening.py` already exercise both middlewares' externally
visible behaviour (headers present, request id echoed, log lines carry the
right id) end-to-end through a real `httpx` client hitting the real `app`,
and both suites still pass unchanged after this rewrite — that's the
"behavior preserved" proof. What those tests can't tell you is *how* the
middleware is implemented: a plain 200 response looks identical whether it
went through `BaseHTTPMiddleware` or pure ASGI. These tests pin the actual
point of the fix instead — that the classes are no longer
`BaseHTTPMiddleware` subclasses at all — plus one behavior that's easy to
get wrong when hand-rolling ASGI message passing: `SecurityHeadersMiddleware`
must never overwrite a header a downstream handler already set explicitly
(the old code's `response.headers.setdefault(...)` semantics).
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware

from app.api.middleware import RequestContextMiddleware, SecurityHeadersMiddleware


def test_request_context_middleware_is_not_a_basehttpmiddleware_subclass():
    assert not issubclass(RequestContextMiddleware, BaseHTTPMiddleware)


def test_security_headers_middleware_is_not_a_basehttpmiddleware_subclass():
    assert not issubclass(SecurityHeadersMiddleware, BaseHTTPMiddleware)


def _http_scope(path: str = "/whatever") -> dict:
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [],
        "query_string": b"",
    }


async def _noop_receive():
    return {"type": "http.request", "body": b"", "more_body": False}


class _CollectingSend:
    """Records every ASGI message a middleware (or the app under it) sends,
    so a test can inspect the final response.start headers without needing
    a real HTTP client."""

    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def __call__(self, message: dict) -> None:
        self.messages.append(message)

    @property
    def response_headers(self) -> dict[bytes, bytes]:
        start = next(m for m in self.messages if m["type"] == "http.response.start")
        return dict(start["headers"])


async def test_security_headers_middleware_adds_every_baseline_header():
    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    mw = SecurityHeadersMiddleware(downstream)
    send = _CollectingSend()
    await mw(_http_scope(), _noop_receive, send)

    headers = send.response_headers
    assert headers[b"x-content-type-options"] == b"nosniff"
    assert headers[b"x-frame-options"] == b"DENY"
    assert headers[b"referrer-policy"] == b"no-referrer"
    assert headers[b"cache-control"] == b"no-store"
    assert headers[b"content-security-policy"] == b"default-src 'none'; frame-ancestors 'none'"
    assert headers[b"strict-transport-security"] == b"max-age=31536000; includeSubDomains"


async def test_security_headers_middleware_never_overwrites_a_header_the_route_already_set():
    """The `setdefault` half specifically — a route that has its own opinion
    about, say, Cache-Control (a static asset wanting to be cached, if one
    ever exists) must win over the blanket `no-store` default."""

    async def downstream(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"cache-control", b"public, max-age=3600")],
            }
        )
        await send({"type": "http.response.body", "body": b"{}"})

    mw = SecurityHeadersMiddleware(downstream)
    send = _CollectingSend()
    await mw(_http_scope(), _noop_receive, send)

    headers = send.response_headers
    assert headers[b"cache-control"] == b"public, max-age=3600"
    # every other baseline header is still added — only the one the route
    # actually set is left alone
    assert headers[b"x-content-type-options"] == b"nosniff"


async def test_security_headers_middleware_passes_through_non_http_scopes_untouched():
    """Lifespan and websocket scopes must never be touched — the old
    `BaseHTTPMiddleware` version only ever saw `http` requests too, by
    Starlette's own dispatch; the pure-ASGI rewrite has to opt out of
    non-http scopes explicitly instead of getting that for free."""
    calls = []

    async def downstream(scope, receive, send):
        calls.append(scope["type"])

    mw = SecurityHeadersMiddleware(downstream)
    await mw({"type": "lifespan"}, _noop_receive, _CollectingSend())
    assert calls == ["lifespan"]


async def test_request_context_middleware_echoes_a_wellformed_inbound_request_id():
    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    mw = RequestContextMiddleware(downstream)
    send = _CollectingSend()
    scope = _http_scope()
    scope["headers"] = [(b"x-request-id", b"mobile-client-trace-abc123")]
    await mw(scope, _noop_receive, send)

    assert send.response_headers[b"x-request-id"] == b"mobile-client-trace-abc123"


async def test_request_context_middleware_mints_a_fresh_id_for_a_malformed_inbound_one():
    """AAD-SEC-006: an inbound id that doesn't match the expected shape is
    replaced, never echoed back or logged verbatim."""

    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    mw = RequestContextMiddleware(downstream)
    send = _CollectingSend()
    scope = _http_scope()
    scope["headers"] = [(b"x-request-id", b"../../etc/passwd\nX-Injected: yes")]
    await mw(scope, _noop_receive, send)

    echoed = send.response_headers[b"x-request-id"]
    assert echoed != b"../../etc/passwd\nX-Injected: yes"
    assert len(echoed) == 16  # uuid4().hex[:16]


async def test_request_context_middleware_sets_request_state_for_downstream_handlers():
    """`app/api/route.py` and the exception handlers in `main.py` both read
    `request.state.request_id` — this is what used to be
    `request.state.request_id = request_id` inside `dispatch()`; the
    pure-ASGI version sets it on `scope["state"]` instead, which is exactly
    what `Request.state` is a thin wrapper around."""
    seen = {}

    async def downstream(scope, receive, send):
        from fastapi import Request

        seen["request_id"] = Request(scope).state.request_id
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    mw = RequestContextMiddleware(downstream)
    await mw(_http_scope(), _noop_receive, _CollectingSend())

    assert seen["request_id"] is not None
    assert len(seen["request_id"]) == 16
