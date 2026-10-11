"""Driving score: a transparent, explainable formula (no black box).

    points_lost(type) = weight(type) x count(type) x 10 / max(distance_km, 5)
    score             = clamp(100 - sum(points_lost), 0, 100)

i.e. weighted events per 10 km. The 5 km floor stops one event on a very short trip
from wiping out the whole score. Weights reflect crash risk: harsh braking and harsh
turns matter more than a slightly early acceleration.
"""
from __future__ import annotations

EVENT_INFO: dict[str, dict] = {
    "OSL": {"name": "Over Speed Limit", "weight": 3,
            "tip": "Chance of risk - avoid exceeding the posted speed limit of the road.",
            "good": "Keep it up! You stayed within speed limits."},
    "ABT": {"name": "Accelerating Before Turn", "weight": 2,
            "tip": "Almost there! Try entering turns at a steady, lower speed.",
            "good": "Keep it up! You enter turns at a steady speed."},
    "BAT": {"name": "Braking After Turn", "weight": 2,
            "tip": "Almost there! Finish braking before the turn and exit at a steady speed.",
            "good": "Keep it up! You exit turns smoothly."},
    "FAB": {"name": "Frequent Acceleration & Braking", "weight": 3,
            "tip": "Try maintaining a steady speed and a longer gap to the car ahead.",
            "good": "Keep it up! Your speed is steady and predictable."},
    "HA": {"name": "Harsh Acceleration", "weight": 3,
           "tip": "Press the accelerator gradually - it is safer and saves fuel.",
           "good": "Keep it up! Gradual increase in speed improves safety and comfort."},
    "HB": {"name": "Harsh Braking", "weight": 5,
           "tip": "Look further ahead and brake early and gently.",
           "good": "Keep it up! Gradual decrease in speed improves safety and comfort."},
    "HT": {"name": "Harsh Turn", "weight": 4,
           "tip": "Slow down before the corner; take turns at a lower, steady speed.",
           "good": "Keep it up! Maintaining proper speed at turns improves comfort and stability."},
}
EVENT_ORDER = ["OSL", "ABT", "BAT", "FAB", "HA", "HB", "HT"]
DISTANCE_FLOOR_KM = 5.0


def penalty_breakdown(counts: dict[str, int], distance_km: float) -> list[dict]:
    scale = 10.0 / max(distance_km, DISTANCE_FLOOR_KM)
    rows = []
    for etype in EVENT_ORDER:
        n = int(counts.get(etype, 0))
        if n:
            info = EVENT_INFO[etype]
            rows.append({"type": etype, "name": info["name"], "count": n,
                         "points": round(info["weight"] * n * scale, 1)})
    return sorted(rows, key=lambda r: -r["points"])


def trip_score(counts: dict[str, int], distance_km: float) -> int:
    lost = sum(r["points"] for r in penalty_breakdown(counts, distance_km))
    return int(round(max(0.0, min(100.0, 100.0 - lost))))


def score_band(score: float | None) -> str:
    if score is None:
        return "No data"
    if score >= 100:
        return "Excellent"
    if score >= 85:
        return "Good"
    if score >= 75:
        return "Fair"
    return "Okay"


def event_level(count: int, distance_km: float) -> int:
    """0 = none (tick), 1-3 = how often it happens per 100 km."""
    if count == 0:
        return 0
    per100 = count * 100.0 / max(distance_km, 1.0)
    if per100 < 5:
        return 1
    if per100 < 15:
        return 2
    return 3
