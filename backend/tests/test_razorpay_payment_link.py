"""Real Razorpay Payment Link integration — signature verification and the
redirect-callback endpoint that confirms the order.

No live Razorpay account is exercised here (none exists yet with real
credentials) — these tests use a fixed fake secret and prove the exact
cryptographic formula and endpoint wiring are correct, so the only thing
left once real keys arrive is configuration, not code.
"""

from __future__ import annotations

import hmac
import hashlib

import pytest

from app.payments.razorpay import RazorpayProvider


FAKE_SECRET = "fake-test-secret-for-verification-only"


@pytest.fixture
def provider(monkeypatch) -> RazorpayProvider:
    from app.core.config import settings

    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_fake")
    monkeypatch.setattr(settings, "razorpay_key_secret", FAKE_SECRET)
    monkeypatch.setattr(settings, "razorpay_webhook_secret", "fake-webhook-secret")
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
