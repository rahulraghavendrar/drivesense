"""Small geo helpers: distance, bearing and an offline 'reverse geocoder'."""
from __future__ import annotations

import math

EARTH_RADIUS_M = 6_371_000.0

# Landmarks around Dearborn, Michigan (Ford's home town). Used to label trip
# start/end points without calling an external geocoding API.
PLACES: list[tuple[str, float, float]] = [
    ("Ford World Headquarters", 42.3157, -83.2104),
    ("Fairlane Town Center", 42.3191, -83.2169),
    ("University of Michigan-Dearborn", 42.3172, -83.2307),
    ("The Henry Ford Museum", 42.3034, -83.2340),
    ("Dearborn Transit Center", 42.3068, -83.2386),
    ("West Dearborn Downtown", 42.3224, -83.2453),
    ("Ford Rouge Complex", 42.3005, -83.1625),
    ("East Dearborn (Warren Ave)", 42.3435, -83.1755),
]


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def describe_location(lat: float, lon: float) -> str:
    """'Fairlane Town Center' if close to a landmark, else '1.2 km from Fairlane Town Center'."""
    name, dist = min(((n, haversine_m(lat, lon, plat, plon)) for n, plat, plon in PLACES), key=lambda x: x[1])
    if dist < 300:
        return f"{name}, Dearborn"
    return f"{dist / 1000:.1f} km from {name}"
