"""Wire models for the delivery-agent flow.

DeliveryOrderView is deliberately leaner than the customer-facing
OrderView — no payment details, no full timeline. An agent needs to know
where to go and what they're carrying, not the customer's payment method.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from app.domain.enums import OrderStatus
from app.domain.geo import in_hyderabad_bounds
from app.schemas.auth import Address
from app.schemas.common import Schema


class DeliveryOrderView(Schema):
    """The full view: full address, notes, order value. Used once an agent
    is actually accountable for the order — after accept, and for the
    ongoing list — never before (see DeliveryRequestView, AAD-SEC-030)."""

    id: str
    order_number: str
    status: str
    address: Address
    notes: str
    total_paise: int
    item_count: int
    created_at: datetime
    delivery_assigned_at: datetime | None = None
    # None when the order's address has no coordinates (an old order, or an
    # address that was typed by hand without ever using "current location").
    # Never used to hide the order — only to say "distance unknown" instead
    # of guessing a number.
    distance_km: float | None = None


class DeliveryRequestView(Schema):
    """AAD-SEC-030: what an agent needs to decide whether to take a job —
    a distance, an item count, how long it's been waiting — and nothing
    that identifies the customer or what they're worth. No address, no
    notes (which routinely restate address detail — "leave at the gate
    near X"), no order value. The full picture (DeliveryOrderView) is
    revealed only on accept, which is also the point an agent becomes
    accountable for having it."""

    id: str
    order_number: str
    item_count: int
    created_at: datetime
    distance_km: float | None = None


class AgentLocationUpdate(Schema):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)

    @model_validator(mode="after")
    def _check_plausible_location(self) -> AgentLocationUpdate:
        # AAD-SEC-028: the ge/le bounds above only reject "off the planet"
        # values. This rejects "on the planet, but nowhere near this farm's
        # operation" — a different city, or a spoofed coordinate picked at
        # random within the global range.
        if not in_hyderabad_bounds(self.latitude, self.longitude):
            raise ValueError(
                "That location is outside the delivery area. "
                "Check location permissions and try again."
            )
        return self


class UpdateDeliveryStatusRequest(Schema):
    # Only packed / out_for_delivery / delivered are ever accepted here —
    # DeliveryService.update_status rejects anything else (confirming,
    # cancelling, refunding stay staff/admin actions via /admin).
    status: OrderStatus
    note: str = Field(default="", max_length=200)


class VerifyDeliveryCodeRequest(Schema):
    """AAD-SEC-027. The 4-digit code the customer's own app shows them —
    `pattern` rejects anything that isn't exactly 4 digits before this ever
    reaches OrderService.verify_delivery_code, so a malformed guess costs
    nothing against the attempt cap."""

    code: str = Field(pattern=r"^\d{4}$")


class ReassignDeliveryRequest(Schema):
    """AAD-REL-006. agent_id=None sends the order back to the unassigned
    pool instead of handing it to someone specific."""

    agent_id: str | None = None
    note: str = Field(default="", max_length=200)
