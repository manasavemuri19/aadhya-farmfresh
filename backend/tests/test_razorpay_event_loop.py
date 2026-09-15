"""Regression tests for AAD-PAY-008.

`RazorpayProvider.create_order`/`poll_status`/`refund` are declared `async`
but, before this fix, called the `razorpay` SDK directly — which wraps
`requests`, a synchronous library — right there on the event loop, with no
timeout configured anywhere (`requests`' own default is `None`, meaning
"wait forever"). `create_order` runs on every single online checkout, so a
slow or hung Razorpay round trip stalled *every* concurrent request this
worker was serving — the catalog, other checkouts, the webhook confirming
someone else's payment — not just the one that triggered it. A connection
that hung rather than failed took the worker out permanently.

Two things prove the fix, and neither is provable by asserting on a return
value: the SDK call must no longer run on the loop itself (`test_...
does_not_block_the_event_loop`), and a connection that never answers must
still be aborted, not hang forever (`test_...hung_gateway_connection_is_
aborted_by_the_timeout`, against a real local server that genuinely never
responds — not a mock, since a monkeypatched `payment_link.create` never
touches `_TimeoutSession` at all and would prove nothing about the timeout).
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from pydantic import SecretStr

from app.core.errors import UpstreamError
from app.payments.razorpay import RazorpayProvider, _TimeoutSession


@pytest.fixture
def provider(monkeypatch) -> RazorpayProvider:
    from app.core.config import settings

    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_fake")
    monkeypatch.setattr(settings, "razorpay_key_secret", SecretStr("fake-test-secret"))
    monkeypatch.setattr(settings, "razorpay_webhook_secret", SecretStr("fake-webhook-secret"))
    return RazorpayProvider()


def _order_kwargs(**overrides):
    kwargs = {"amount_paise": 10_000, "currency": "INR", "receipt": "ord_test", "notes": {}}
    kwargs.update(overrides)
    return kwargs


# ---------------------------------------------------------------------------
# The SDK call must not run on the event loop itself
# ---------------------------------------------------------------------------


async def test_create_order_does_not_block_the_event_loop(provider, monkeypatch):
    """A slow gateway call must not stall other concurrent work this worker
    is serving. Proven by racing a lightweight heartbeat *task* against a
    deliberately slow (but real, blocking) SDK call, and counting only the
    ticks that land *during* the call — not after, since gathering both to
    completion and checking the final count would pass either way (the
    heartbeat finishes eventually regardless of whether it was blocked in
    the meantime). A plain `time.sleep` inside a coroutine, awaited
    directly rather than via `to_thread`, never yields to the loop at all,
    so a genuinely blocked loop leaves the heartbeat at 0 ticks here — not
    just fewer ticks, none, because the scheduler never got a turn."""

    def slow_create(body):
        time.sleep(0.3)
        return {"id": "plink_slow", "short_url": "https://rzp.io/l/slow"}

    monkeypatch.setattr(provider._client.payment_link, "create", slow_create)

    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    hb_task = asyncio.create_task(heartbeat())
    order = await provider.create_order(**_order_kwargs())
    hb_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await hb_task

    assert order.provider_order_id == "plink_slow"
    # ~0.3s of slow_create at a 10ms heartbeat cadence should land in the
    # high teens to ~30 ticks if the loop stayed free the whole time.
    assert ticks >= 15, f"heartbeat only ticked {ticks} times — event loop was blocked"


def test_client_is_constructed_with_a_default_timeout(provider):
    """Guards the `__init__` wiring itself. The two timeout tests below
    replace `_client.session` directly so they can control their own
    timing — on their own they'd miss a regression where `__init__` stops
    wiring `_TimeoutSession` in by default, which is exactly what every
    real call through this client actually depends on."""
    assert isinstance(provider._client.session, _TimeoutSession)
    assert provider._client.session._default_timeout == RazorpayProvider._TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# A hung connection must still be aborted, not wait forever
# ---------------------------------------------------------------------------


class _HangingHandler(BaseHTTPRequestHandler):
    """Accepts the connection like a real server, then never answers —
    the exact condition a `None` (unset) requests timeout waits on forever."""

    def do_POST(self):  # noqa: N802 - required name by http.server
        time.sleep(5)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):  # keep test output quiet
        pass


@pytest.fixture
def hanging_server():
    server = HTTPServer(("127.0.0.1", 0), _HangingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=2)


async def test_a_hung_gateway_connection_is_aborted_by_the_timeout(provider, hanging_server):
    port = hanging_server.server_address[1]
    provider._client.base_url = f"http://127.0.0.1:{port}"
    # A short timeout keeps this test fast; production uses
    # RazorpayProvider._TIMEOUT_SECONDS (10s) — the mechanism under test is
    # identical, only the bound differs.
    provider._client.session = _TimeoutSession(timeout=0.4)

    started = time.monotonic()
    with pytest.raises(UpstreamError):
        await provider.create_order(**_order_kwargs())
    elapsed = time.monotonic() - started

    # Bounded by the timeout, not by the server's 5s sleep or the test
    # runner having to be killed — that gap is exactly what "requests'
    # default timeout is None" meant before this fix.
    assert elapsed < 2.0, f"took {elapsed:.2f}s — timeout was not applied"


async def test_refund_also_honours_the_timeout(provider, hanging_server):
    """The same fix covers all three call sites, not just create_order."""
    port = hanging_server.server_address[1]
    provider._client.base_url = f"http://127.0.0.1:{port}"
    provider._client.session = _TimeoutSession(timeout=0.4)

    started = time.monotonic()
    with pytest.raises(UpstreamError):
        await provider.refund(provider_payment_id="pay_x", amount_paise=1000, notes={})
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f"took {elapsed:.2f}s — timeout was not applied"
