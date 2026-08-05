from __future__ import annotations

import pytest

from app.schemas.auth import normalise_phone


@pytest.mark.parametrize(
    "raw",
    ["9876543210", "+919876543210", "919876543210", "09876543210",
     "98765 43210", "+91 98765-43210"],
)
def test_all_common_formats_normalise_to_one_canonical_value(raw):
    assert normalise_phone(raw) == "+919876543210"


@pytest.mark.parametrize(
    "raw",
    ["1234567890",     # cannot start with 1
     "987654321",      # too short
     "98765432101",    # too long
     "5876543210",     # invalid leading digit
     "abcdefghij"],
)
def test_invalid_numbers_are_rejected(raw):
    with pytest.raises(ValueError):
        normalise_phone(raw)
