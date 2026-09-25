"""AAD-QUAL-005 / AAD-QUAL-020 / AAD-QUAL-031 / AAD-QUAL-022 — bare `assert`
statements in production code, all fixed the same way.

`python -O` strips every `assert` in the interpreter, and even when asserts
are on, an `AssertionError` on its own says nothing about which invariant
broke or why. Three instances of `assert x is not None` (two in
`UserRepository`, one in `OrderService`) — each guarding a "we just wrote
this row, re-reading it now should be impossible to come back empty"
invariant — are now explicit `if x is None: raise RuntimeError(...)`
checks with a diagnostic message, so the check always runs and, on the day
it's ever wrong, says exactly what vanished.

A fourth instance — `DeliveryService.update_status`'s own re-read of the
order after a status change, which `AAD-QUAL-031` gave this same treatment
— no longer exists at all as of `AAD-PERF-013`: that fix removed the
re-read itself (the caller now reuses the `OrderView` `OrderService.
update_status` already returns, instead of asking the database for the
same order a third time), so there's nothing left to guard and nothing
left to test here. Its test is gone along with the code path, not left
behind as dead coverage of a branch that can no longer run.

A fifth and sixth instance (`assert isinstance(payments, ConcreteProvider)`
in two payment routes) were a different shape: pure type-narrowing for
mypy, not a data invariant. Those are fixed by moving the two provider-
specific methods onto the `PaymentProvider` base class itself (default
implementation: raise `NotImplementedError`), so the routes can call them
directly with no `isinstance` check needed at all.

These tests force each "impossible" branch with `monkeypatch` (the only way
to exercise a branch the application itself is structured to prevent), and
separately prove the two provider methods raise a clear error on the
provider that doesn't implement them.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from app.domain.enums import PaymentMethod
from app.payments.mock import MockPaymentProvider
from app.payments.razorpay import RazorpayProvider
from app.repositories.users import UserRepository
from app.schemas.auth import Address
from app.schemas.order import CartLineInput, CreateOrderRequest
from app.services.order_service import OrderService

ADDRESS = Address(
    label="Home", line1="12-3-45 Banjara Hills", city="Hyderabad", pincode="500034"
)


def order_request(lines, **kw) -> CreateOrderRequest:
    return CreateOrderRequest(
        lines=[CartLineInput(sku=s, qty=q) for s, q in lines], address=ADDRESS, **kw,
    )


# ---------- AAD-QUAL-005 ----------


class TestUserRepositoryInvariants:
    async def test_get_or_create_by_google_raises_explicit_error_if_row_vanishes(
        self, session, monkeypatch
    ):
        repo = UserRepository(session)
        monkeypatch.setattr(repo, "get_by_google_sub", AsyncMock(return_value=None))

        with pytest.raises(RuntimeError, match="vanished"):
            await repo.get_or_create_by_google(
                google_sub="ghost_sub", email="ghost@example.com", name="Ghost"
            )

    async def test_the_ordinary_path_still_returns_the_real_user_unaffected(self, session):
        """Sanity check that the fix didn't change the happy path."""
        repo = UserRepository(session)
        created = await repo.get_or_create_by_google(
            google_sub="invariant_ordinary_path", email="ok@example.com", name="Ok",
        )
        assert created["email"] == "ok@example.com"

        again = await repo.get_or_create_by_google(
            google_sub="invariant_ordinary_path", email="ok@example.com", name="Ok",
        )
        assert again["id"] == created["id"]

    def test_get_or_create_by_phone_is_gone_not_just_fixed(self):
        """AAD-QUAL-005: writing the test above for this method's sibling
        surfaced that `get_or_create_by_phone` was already dead *and*
        broken — its `on_conflict_do_nothing` targets a `phone` unique
        index that no longer exists (phone/OTP login was retired in favour
        of Google sign-in). Zero remaining callers anywhere in the app
        (confirmed by grep), so it's deleted rather than patched, the same
        call `AAD-SEC-016` made for the dead Argon2 helpers."""
        import app.repositories.users as users_module

        assert not hasattr(users_module.UserRepository, "get_or_create_by_phone")


# ---------- AAD-QUAL-020 ----------


async def test_create_order_raises_explicit_error_if_the_order_vanishes(
    order_service: OrderService, user, milk, monkeypatch
):
    monkeypatch.setattr(order_service.orders, "get", AsyncMock(return_value=None))

    with pytest.raises(RuntimeError, match="vanished"):
        await order_service.create_order(
            user_id=user["id"],
            request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
            idempotency_key="invariant-test-order-vanishes",
        )


# ---------- AAD-QUAL-022 ----------


class TestPaymentProviderMethodsNoLongerNeedIsinstance:
    def test_mock_provider_implements_sign_for_testing(self):
        provider = MockPaymentProvider()
        signature = provider.sign_for_testing("order_1", "pay_1")
        assert isinstance(signature, str) and signature

    def test_mock_provider_does_not_implement_the_razorpay_only_method(self):
        provider = MockPaymentProvider()
        with pytest.raises(NotImplementedError, match="mock"):
            provider.verify_payment_link_callback(
                payment_link_id="x",
                payment_link_reference_id="y",
                payment_link_status="paid",
                payment_id="z",
                signature="sig",
            )

    def test_razorpay_provider_does_not_implement_the_mock_only_method(self, monkeypatch):
        monkeypatch.setattr("app.core.config.settings.razorpay_key_id", "rzp_test_fake")
        monkeypatch.setattr(
            "app.core.config.settings.razorpay_key_secret", SecretStr("fake-secret")
        )
        monkeypatch.setattr(
            "app.core.config.settings.razorpay_webhook_secret", SecretStr("fake-webhook")
        )
        monkeypatch.setattr(
            "app.core.config.settings.razorpay_callback_url",
            "https://example.test/v1/payments/link-redirect",
        )
        provider = RazorpayProvider()
        with pytest.raises(NotImplementedError, match="razorpay"):
            provider.sign_for_testing("order_1", "pay_1")
