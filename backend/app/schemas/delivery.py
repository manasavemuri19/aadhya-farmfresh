"""Wire models for the delivery-agent flow.

DeliveryOrderView is deliberately leaner than the customer-facing
OrderView — no payment details, no full timeline. An agent needs to know
where to go and what they're carrying, not the customer's payment method.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.schemas.auth import Address
from app.schemas.common import Schema


class DeliveryOrderView(Schema):
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


class AgentLocationUpdate(Schema):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
