"""AAD-SEC-009 — `UpdateProfile.phone` now runs the same normalisation the
login path always has, instead of accepting any 16 characters."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.auth import UpdateProfile


@pytest.mark.parametrize(
    "raw",
    ["9876543210", "+919876543210", "919876543210", "09876543210"],
)
def test_valid_indian_numbers_normalise(raw):
    assert UpdateProfile(phone=raw).phone == "+919876543210"


@pytest.mark.parametrize(
    "raw",
    ["not a number", "<script>alert(1)</script>", "12345", "0000000000"],
)
def test_garbage_or_invalid_numbers_are_rejected(raw):
    with pytest.raises(ValidationError):
        UpdateProfile(phone=raw)


def test_phone_is_still_optional():
    profile = UpdateProfile(name="Just a name change")
    assert profile.phone is None


def test_explicit_none_stays_none():
    assert UpdateProfile(phone=None).phone is None
