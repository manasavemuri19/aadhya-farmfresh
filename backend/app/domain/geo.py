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
