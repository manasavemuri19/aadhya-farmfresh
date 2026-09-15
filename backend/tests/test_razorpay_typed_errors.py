"""AAD-PAY-013 — `create_order` and `refund` used to catch every exception
and map it to the same `UpstreamError` ("Could not start the payment. Try
again."). That's wrong for a `razorpay.errors.BadRequestError`: it means
Razorpay rejected the request we sent — a malformed amount, an unsupported
currency, bad auth from a rotated key — which is our own bug, not a
transient gateway problem, and retrying the identical request fails
identically. These tests prove `BadRequestError` now propagates on its own
rather than being disguised as `UpstreamError`, while a genuinely transient
failure (anything else, including the SDK's own `ServerError`/`GatewayError`)
still becomes `UpstreamError` exactly as before.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr
from razorpay.errors import BadRequestError, ServerError

from app.core.errors import UpstreamError
from app.payments.razorpay import RazorpayProvider


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


async def test_create_order_lets_a_bad_request_error_propagate_unwrapped(provider, monkeypatch):
    def broken_create(body):
        raise BadRequestError("The amount must be at least INR 1.")

    monkeypatch.setattr(provider._client.payment_link, "create", broken_create)

    with pytest.raises(BadRequestError):
        await provider.create_order(**_order_kwargs())


async def test_create_order_still_wraps_a_genuinely_transient_failure(provider, monkeypatch):
    def broken_create(body):
        raise ServerError("Razorpay is having a bad day.")

    monkeypatch.setattr(provider._client.payment_link, "create", broken_create)

    with pytest.raises(UpstreamError):
        await provider.create_order(**_order_kwargs())


async def test_refund_lets_a_bad_request_error_propagate_unwrapped(provider, monkeypatch):
    def broken_refund(payment_id, data):
        raise BadRequestError("Refund amount exceeds the amount captured.")

    monkeypatch.setattr(provider._client.payment, "refund", broken_refund)

    with pytest.raises(BadRequestError):
        await provider.refund(provider_payment_id="pay_x", amount_paise=1000, notes={})


async def test_refund_still_wraps_a_genuinely_transient_failure(provider, monkeypatch):
    def broken_refund(payment_id, data):
        raise ServerError("Razorpay is having a bad day.")

    monkeypatch.setattr(provider._client.payment, "refund", broken_refund)

    with pytest.raises(UpstreamError):
        await provider.refund(provider_payment_id="pay_x", amount_paise=1000, notes={})
