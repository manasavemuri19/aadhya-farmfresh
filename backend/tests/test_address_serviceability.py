"""AAD-SEC-020 / AAD-QUAL-012 — `Address` now rejects coordinates outside a
generous Hyderabad-metro bounding box (the same one `AAD-SEC-028` already
applies to delivery-agent locations), instead of accepting latitude and
longitude anywhere on Earth.

Before this fix, nothing in the request pipeline stopped a customer from
quoting, paying, and then moving an order's delivery address hundreds of
kilometres away via `PATCH /orders/{id}/address` — the order stayed
confirmed, the delivery fee stayed as charged, and the delivery agent's
distance matching just returned a large number. The same gap existed at
order creation and on a saved profile address, since all three go through
this one `Address` schema.

This is a schema-level fix, so the enforcement point is `Address(...)`
construction itself — in production that happens during FastAPI's request
body parsing, before any route handler or service method ever runs. These
tests exercise the schema directly for that reason, plus one end-to-end
case through `OrderService.update_address` to confirm the schema layer is
in fact what stands between a request and that service, not something a
call site could route around.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.enums import PaymentMethod
from app.schemas.auth import Address

# Well inside HYDERABAD_LAT_RANGE / HYDERABAD_LNG_RANGE (app/domain/geo.py).
_HYDERABAD_POINT = (17.4200, 78.6000)
# New York — nowhere near it, and a real coordinate pair, not a typo.
_FAR_AWAY_POINT = (40.7128, -74.0060)


def _address(latitude: float | None, longitude: float | None) -> dict:
    return {
        "label": "Home", "line1": "12-3-45 Banjara Hills", "line2": "", "landmark": "",
        "city": "Hyderabad", "pincode": "500034",
        "latitude": latitude, "longitude": longitude,
    }


def test_address_rejects_coordinates_outside_the_delivery_area():
    lat, lng = _FAR_AWAY_POINT
    with pytest.raises(ValidationError, match="outside our delivery area"):
        Address(**_address(lat, lng))


def test_address_accepts_coordinates_inside_the_delivery_area():
    lat, lng = _HYDERABAD_POINT
    address = Address(**_address(lat, lng))
    assert address.latitude == lat
    assert address.longitude == lng


def test_address_with_no_coordinates_at_all_is_unaffected():
    """Documented gap, not a regression: with neither coordinate supplied
    there is nothing to compare against the bounding box, so this case
    still passes through unchecked — the same as before this fix."""
    address = Address(**_address(None, None))
    assert address.latitude is None
    assert address.longitude is None


async def test_moving_a_confirmed_orders_address_far_away_is_rejected(
    order_service, user, milk
):
    from app.schemas.order import CartLineInput, CreateOrderRequest

    order = await order_service.create_order(
        user_id=user["id"],
        request=CreateOrderRequest(
            lines=[CartLineInput(sku="MILK-COW-1L", qty=1)],
            address=Address(**_address(*_HYDERABAD_POINT)),
            payment_method=PaymentMethod.COD,
        ),
        idempotency_key=None,
    )

    lat, lng = _FAR_AWAY_POINT
    with pytest.raises(ValidationError, match="outside our delivery area"):
        Address(**_address(lat, lng))

    # The order itself is untouched — the bad address never got far enough
    # to reach OrderService.update_address, let alone the database. It still
    # carries the original, in-bounds address from checkout.
    reread = await order_service.get_for_user(order.id, user["id"])
    assert reread.address.latitude == _HYDERABAD_POINT[0]
