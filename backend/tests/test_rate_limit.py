"""AAD-SEC-004 (rate limiting) and AAD-SEC-005 (trusted client IP) — unit
coverage of the limiter and the IP resolver in isolation, fast and
deterministic rather than needing hundreds of real HTTP round trips to
exhaust a 60-or-120-request window. `test_edge_hardening.py` covers the
actual wiring (a real request over the limit gets a real 429).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.client_ip import get_client_ip
from app.core.errors import RateLimited
from app.core.rate_limit import RateLimiter


def _request(*, forwarded_for: str | None = None, client_host: str | None = "203.0.113.9"):
    headers = {"x-forwarded-for": forwarded_for} if forwarded_for else {}
    client = SimpleNamespace(host=client_host) if client_host else None
    return SimpleNamespace(headers=headers, client=client)


def test_client_ip_trusts_only_the_rightmost_forwarded_for_hop():
    """A client that sends its own X-Forwarded-For can only ever forge the
    hops to the *left* of whatever Railway's single proxy hop appended."""
    request = _request(forwarded_for="1.2.3.4, 5.6.7.8, 9.9.9.9")
    assert get_client_ip(request) == "9.9.9.9"


def test_client_ip_handles_a_single_hop():
    request = _request(forwarded_for="198.51.100.7")
    assert get_client_ip(request) == "198.51.100.7"


def test_client_ip_falls_back_to_request_client_when_header_absent():
    request = _request(forwarded_for=None, client_host="10.0.0.5")
    assert get_client_ip(request) == "10.0.0.5"


def test_client_ip_falls_back_to_unknown_when_nothing_is_available():
    request = _request(forwarded_for=None, client_host=None)
    assert get_client_ip(request) == "unknown"


def test_limiter_allows_up_to_the_limit_then_rejects():
    limiter = RateLimiter(limit=3, seconds=60)
    limiter.check("same-key")
    limiter.check("same-key")
    limiter.check("same-key")
    with pytest.raises(RateLimited):
        limiter.check("same-key")


def test_limiter_rejection_carries_a_retry_after_header():
    limiter = RateLimiter(limit=1, seconds=60)
    limiter.check("k")
    with pytest.raises(RateLimited) as exc_info:
        limiter.check("k")
    assert "Retry-After" in exc_info.value.headers
    assert int(exc_info.value.headers["Retry-After"]) > 0


def test_limiter_keys_are_independent():
    """Two different callers must not share one bucket."""
    limiter = RateLimiter(limit=1, seconds=60)
    limiter.check("alice")
    limiter.check("bob")  # must not raise — different key, fresh bucket


def test_limiter_window_slides_rather_than_resetting_at_a_boundary(monkeypatch):
    """A sliding window, not a fixed bucket that resets unfairly at a
    wall-clock edge — advance monotonic time past the window and the same
    key is allowed again."""
    import app.core.rate_limit as rate_limit_module

    fake_now = [1000.0]
    monkeypatch.setattr(rate_limit_module.time, "monotonic", lambda: fake_now[0])

    limiter = RateLimiter(limit=1, seconds=10)
    limiter.check("k")
    with pytest.raises(RateLimited):
        limiter.check("k")

    fake_now[0] += 11  # past the 10-second window
    limiter.check("k")  # must not raise now
