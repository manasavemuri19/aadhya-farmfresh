"""In-process rate limiting (AAD-SEC-004).

A fixed-window counter per key, held in a plain in-memory dict — no new
infrastructure dependency. `slowapi` and a Redis token-bucket were the
audit's two suggested options; this codebase declares neither Redis nor
`slowapi` today, and the Dockerfile's `CMD` runs a single uvicorn process
with no `--workers` flag, so there is only ever one process per Railway
replica to hold these counters in. That is a deliberate, scoped choice, not
an oversight: **this is correct for exactly one replica.** The moment this
service runs more than one Railway replica, each gets its own counters and
the effective limit multiplies by replica count — the same caveat
`app/main.py`'s `_housekeeping` docstring already states for the in-process
sweeper, and the same fix applies: move to a shared store (Redis `INCR` +
`EXPIRE` is the standard shape) at that point. Flagged here so it is not
forgotten silently — see the open question this review already asks about
replica count.

Every limiter here keys on the client IP `AAD-SEC-005` established as
trustworthy (`get_client_ip`) or on an already-authenticated user id —
never on a header the caller controls.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import Request

from app.core.client_ip import get_client_ip
from app.core.errors import RateLimited

_MESSAGE = "Too many requests. Try again shortly."


class RateLimiter:
    """One fixed window: at most `limit` hits per `seconds`, per key.

    A sliding-window log (a deque of hit timestamps, trimmed to the window
    on every check) rather than a fixed-bucket counter — it doesn't reset
    unfairly at a wall-clock boundary, and it's cheap at this scale: each
    key's deque only ever holds up to `limit` entries.
    """

    def __init__(self, *, limit: int, seconds: int) -> None:
        self._limit = limit
        self._seconds = seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        now = time.monotonic()
        hits = self._hits[key]
        cutoff = now - self._seconds
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) >= self._limit:
            retry_after = max(1, int(hits[0] + self._seconds - now) + 1)
            raise RateLimited(_MESSAGE, headers={"Retry-After": str(retry_after)})
        hits.append(now)


class IpRateLimiter(RateLimiter):
    """FastAPI-dependency form — `Depends(IpRateLimiter(limit=10, seconds=60))`.

    Chain two instances on the same route for two independent windows (the
    fix's own suggestion for `/auth/google`: 10/min *and* 30/hour).
    """

    def __call__(self, request: Request) -> None:
        self.check(get_client_ip(request))
