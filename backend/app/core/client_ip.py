"""Resolve the client IP the proxy in front of this container actually saw.

AAD-SEC-005: the Dockerfile used to launch uvicorn with `--proxy-headers
--forwarded-allow-ips='*'`, which told uvicorn to trust `X-Forwarded-For`
from *any* source — so a client that sent its own header controlled what
`request.client.host` became: a complete, one-line forgery of its own IP.
That flag is gone. uvicorn no longer rewrites `request.client` from proxy
headers at all (nothing in this codebase relied on `request.url.scheme`
being corrected by `X-Forwarded-Proto` either — grepped, confirmed — so
dropping the flag costs nothing else).

Railway sits as a single hop in front of this container: every request
passes through exactly one reverse proxy, which appends the connection's
real peer address as the *last* entry of `X-Forwarded-For`, regardless of
what a client supplied before that. Railway does not publish a fixed CIDR
range for that proxy the way Cloudflare or an AWS ALB does, so pinning
`--forwarded-allow-ips` to a specific range — the audit's other suggested
option — would either be wrong today or silently stop being true after an
infrastructure change on Railway's side, with nothing to catch it.

Trusting exactly the rightmost `X-Forwarded-For` hop sidesteps that: it is
correct regardless of what Railway's own proxy IP happens to be, and it
discards everything to the left of it, which is exactly the part a client
controls. This is the trusted IP source `AAD-SEC-004`'s rate limiter keys
on — never a header taken at face value.
"""

from __future__ import annotations

from fastapi import Request


def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
        if hops:
            return hops[-1]
    return request.client.host if request.client else "unknown"
