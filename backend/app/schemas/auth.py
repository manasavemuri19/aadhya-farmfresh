from __future__ import annotations

import re

from pydantic import Field, field_validator

from app.schemas.common import Schema

# Indian mobile numbers: 10 digits starting 6-9. Stored canonically as +91XXXXXXXXXX.
_INDIAN_MOBILE = re.compile(r"^[6-9]\d{9}$")


def normalise_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    elif digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    if not _INDIAN_MOBILE.match(digits):
        raise ValueError("Enter a 10-digit Indian mobile number")
    return f"+91{digits}"


class TokenPair(Schema):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int


class RefreshRequest(Schema):
    refresh_token: str


class Address(Schema):
    label: str = Field(default="Home", max_length=32)
    line1: str = Field(min_length=4, max_length=160)
    line2: str = Field(default="", max_length=160)
    landmark: str = Field(default="", max_length=120)
    city: str = Field(default="Hyderabad", max_length=80)
    pincode: str = Field(pattern=r"^\d{6}$")
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)


class UserProfile(Schema):
    id: str
    phone: str | None = None
    email: str | None = None
    name: str = ""
    role: str = "customer"
    addresses: list[Address] = []


class UpdateProfile(Schema):
    name: str | None = Field(default=None, max_length=80)
    # AAD-SEC-009: OtpRequest/OtpVerify used to be the only schemas that
    # normalised a phone number — this is the path people actually use now
    # that login is Google-only, and it accepted any 16 characters. Runs the
    # same `normalise_phone` login used to, so "not a number", "<script>",
    # or a non-Indian number are rejected here exactly as they always were
    # there, rather than stored verbatim in the one field used to actually
    # reach a customer about their delivery.
    phone: str | None = Field(default=None, max_length=16)
    address: Address | None = None

    @field_validator("phone")
    @classmethod
    def _normalise_phone(cls, v: str | None) -> str | None:
        return normalise_phone(v) if v is not None else None


class GoogleSignInRequest(Schema):
    id_token: str
