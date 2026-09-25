"""Real Razorpay Payment Link callback — the signature formula in isolation.

No live Razorpay account is exercised here (none exists yet with real
credentials) — these tests use a fixed fake secret and prove the exact
cryptographic formula is correct: which fields go into the signed message,
in which order, and that a tampered or mismatched field fails verification.

This file proves the formula only, not the endpoint. Every test below calls
`provider.verify_payment_link_callback(...)` directly — nothing here sends a
request. The `GET /payments/link-callback` route itself (query parameter
wiring, the conditional webhook application, the response body) and the
`POST /payments/webhook` route (raw-body HMAC verification, the replay
guard) are covered end-to-end, over real ASGI requests, in
`test_payment_endpoints.py` (AAD-OPS-015). `test_payment_link_confirmation.py`
covers `parse_webhook` → `apply_webhook` at the service layer, one level
below the route but still short of an actual request.
"""

from __future__ import annotations

import hmac
import hashlib

import pytest
from pydantic import SecretStr

from app.payments.razorpay import RazorpayProvider


FAKE_SECRET = "fake-test-secret-for-verification-only"


@pytest.fixture
def provider(monkeypatch) -> RazorpayProvider:
    from app.core.config import settings

    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_fake")
    monkeypatch.setattr(settings, "razorpay_key_secret", SecretStr(FAKE_SECRET))
    monkeypatch.setattr(settings, "razorpay_webhook_secret", SecretStr("fake-webhook-secret"))
    monkeypatch.setattr(
        settings, "razorpay_callback_url", "https://example.test/v1/payments/link-redirect"
    )
    return RazorpayProvider()


def _sign(payment_link_id: str, reference_id: str, status: str, payment_id: str) -> str:
    message = f"{payment_link_id}|{reference_id}|{status}|{payment_id}".encode()
    return hmac.new(FAKE_SECRET.encode(), message, hashlib.sha256).hexdigest()


class TestSignatureFormula:
    """This is Razorpay's own documented formula for Payment Links — a
    different field order and set from Standard Checkout's, and it is easy
    to accidentally use the wrong one since both exist in the same SDK."""

    def test_correct_signature_is_accepted(self, provider):
        sig = _sign("plink_ABC", "ord_123", "paid", "pay_XYZ")
        assert provider.verify_payment_link_callback(
            payment_link_id="plink_ABC",
            payment_link_reference_id="ord_123",
            payment_link_status="paid",
            payment_id="pay_XYZ",
            signature=sig,
        ) is True

    def test_tampered_amount_reference_is_rejected(self, provider):
        # Signed for one order, presented for another — must not verify.
        sig = _sign("plink_ABC", "ord_123", "paid", "pay_XYZ")
        assert provider.verify_payment_link_callback(
            payment_link_id="plink_ABC",
            payment_link_reference_id="ord_999",  # different order
            payment_link_status="paid",
            payment_id="pay_XYZ",
            signature=sig,
        ) is False

    def test_status_swap_is_rejected(self, provider):
        # A signature for "paid" must not verify against "failed" — the
        # status is part of what's signed, not free-form metadata.
        sig = _sign("plink_ABC", "ord_123", "paid", "pay_XYZ")
        assert provider.verify_payment_link_callback(
            payment_link_id="plink_ABC",
            payment_link_reference_id="ord_123",
            payment_link_status="failed",
            payment_id="pay_XYZ",
            signature=sig,
        ) is False

    def test_garbage_signature_is_rejected(self, provider):
        assert provider.verify_payment_link_callback(
            payment_link_id="plink_ABC",
            payment_link_reference_id="ord_123",
            payment_link_status="paid",
            payment_id="pay_XYZ",
            signature="0" * 64,
        ) is False

    def test_wrong_secret_produces_a_non_matching_signature(self, provider):
        """A signature made with a different key must not verify — this is
        what actually stops someone from forging their own callback."""
        wrong_message = "plink_ABC|ord_123|paid|pay_XYZ".encode()
        forged = hmac.new(b"not-the-real-secret", wrong_message, hashlib.sha256).hexdigest()
        assert provider.verify_payment_link_callback(
            payment_link_id="plink_ABC",
            payment_link_reference_id="ord_123",
            payment_link_status="paid",
            payment_id="pay_XYZ",
            signature=forged,
        ) is False
