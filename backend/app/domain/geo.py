"""Distance between two points on Earth. One function, no dependencies —
this doesn't need PostGIS or a geo library at the scale of a single farm's
delivery radius.
"""

from __future__ import annotations

from math import asin, cos, radians, sin, sqrt

_EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    lat1_r, lng1_r, lat2_r, lng2_r = map(radians, (lat1, lng1, lat2, lng2))
    d_lat = lat2_r - lat1_r
    d_lng = lng2_r - lng1_r
    a = sin(d_lat / 2) ** 2 + cos(lat1_r) * cos(lat2_r) * sin(d_lng / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * asin(sqrt(a))


# AAD-SEC-028 / AAD-SEC-020: a generous Hyderabad-metro bounding box, used to
# reject a coordinate that couldn't plausibly be a real position for this
# farm's operation — a different city, a spoofed "anywhere on Earth" value,
# or a typo. Deliberately wide (roughly 110km each direction) so it never
# rejects a real delivery or a real agent; it exists to kill the crude
# cases, not to define a precise service boundary. One shared definition so
# AAD-SEC-020 (customer address coordinates), when it's fixed, uses the same
# box as AAD-SEC-028 (agent-reported location) rather than two that drift.
HYDERABAD_LAT_RANGE = (16.9, 17.9)
HYDERABAD_LNG_RANGE = (77.9, 78.9)


def in_hyderabad_bounds(lat: float, lng: float) -> bool:
    lat_min, lat_max = HYDERABAD_LAT_RANGE
    lng_min, lng_max = HYDERABAD_LNG_RANGE
    return lat_min <= lat <= lat_max and lng_min <= lng <= lng_max
