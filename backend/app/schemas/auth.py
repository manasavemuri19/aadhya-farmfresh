from __future__ import annotations

import re

from pydantic import Field, field_validator, model_validator

from app.domain.geo import in_hyderabad_bounds
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
    # AAD-MOB-022: min_length=1 is new — with multiple saved addresses now
    # real (distinct labels, not always "Home"), a blank label would either
    # collide with every other blank-labelled row's own upsert (min_length
    # wasn't previously set, so "" was a technically-valid label before
    # this) or, worse, silently overwrite an address the customer meant to
    # keep, since "" == "" would still satisfy the (user_id, label) unique
    # constraint's own conflict target.
    label: str = Field(default="Home", min_length=1, max_length=32)
    line1: str = Field(min_length=4, max_length=160)
    line2: str = Field(default="", max_length=160)
    landmark: str = Field(default="", max_length=120)
    city: str = Field(default="Hyderabad", max_length=80)
    pincode: str = Field(pattern=r"^\d{6}$")
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)

    # AAD-SEC-020 / AAD-QUAL-012: latitude/longitude used to be accepted
    # across the whole global range with no further check — any real
    # coordinate pair was "valid" as far as this schema was concerned, so a
    # customer could quote, pay, and then move an order 200km away with
    # nothing in the request pipeline ever noticing. Address is the one
    # schema every address-carrying request goes through — order creation,
    # PATCH .../address, and the saved-profile address — so gating it here,
    # once, covers all three call sites the same way `assert_serviceable`
    # called from each of them individually would have, without a second
    # site being able to forget the check later. Deliberately reuses the
    # same generous Hyderabad-metro box AAD-SEC-028 already applies to agent
    # locations (see geo.py) rather than a second, drifting definition.
    #
    # This closes the "anywhere on Earth" hole whenever coordinates are
    # supplied. It does not add a pincode allowlist (the fix this finding
    # also suggested) — that needs the farm's actual list of serviceable
    # pincodes, which is a business input this session doesn't have, not a
    # code change; see AAD-BIZ-001 for the same category of gap. An address
    # saved with no coordinates at all still slips past this check
    # entirely, for the same reason: there is nothing here to compare.
    @model_validator(mode="after")
    def _coordinates_must_be_serviceable(self) -> Address:
        if (
            self.latitude is not None
            and self.longitude is not None
            and not in_hyderabad_bounds(self.latitude, self.longitude)
        ):
            raise ValueError("This address is outside our delivery area right now.")
        return self


class UserProfile(Schema):
    id: str
    phone: str | None = None
    email: str | None = None
    name: str = ""
    role: str = "customer"
    addresses: list[Address] = []


class GoogleSignInResponse(Schema):
    """AAD-API-001: `POST /auth/google` used to declare `-> dict` and hand-call
    `.model_dump()` on each half of its return value — every other route in
    this app validates its response through `response_model`. That meant no
    OpenAPI schema for this endpoint, no response validation, and any field
    later added to `TokenPair` or `UserProfile` would have leaked straight
    into the wire response with no review point. This model just names the
    same two-part shape (`tokens`, `user`) the handler already returned.
    """

    tokens: TokenPair
    user: UserProfile


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
