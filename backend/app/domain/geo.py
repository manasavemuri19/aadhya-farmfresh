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


# AAD-PERF-012: one degree of latitude is ~111.32km everywhere; one degree of
# longitude shrinks toward the poles by a factor of cos(latitude) — at
# Hyderabad's ~17°N that's still close to 1 (~106km), but this doesn't
# assume that and computes it properly.
_KM_PER_DEGREE_LAT = 111.32


def bounding_box_km(lat: float, lng: float, radius_km: float) -> tuple[float, float, float, float]:
    """A rectangular over-approximation of the circle of `radius_km` around
    (lat, lng) — (lat_min, lat_max, lng_min, lng_max).

    This is deliberately generous, not exact: a SQL WHERE clause can cheaply
    narrow "everything on the whole planet" down to "everything in this
    rectangle", and `haversine_km` then gives the real distance (and the
    real yes/no on "within radius") for the much smaller set of rows that
    survive it. The corners of the rectangle are up to ~40% farther from
    the centre than `radius_km` — that's fine, since nothing downstream
    trusts the box itself as the answer.
    """
    lat_delta = radius_km / _KM_PER_DEGREE_LAT
    lng_km_per_degree = max(_KM_PER_DEGREE_LAT * cos(radians(lat)), 1e-6)
    lng_delta = radius_km / lng_km_per_degree
    return (lat - lat_delta, lat + lat_delta, lng - lng_delta, lng + lng_delta)
