"""Slow, multi-day prediction: is the 12 V battery wearing out?

A healthy battery rests at ~12.6 V when the car is parked. A weakening one loses a
little resting voltage every day. We take the daily average resting voltage from the
telemetry hypertable (time_bucket), fit a straight line, and project when it will fall
below 12.0 V - roughly where cold starts begin to fail. That gives the driver days of
warning instead of a car that will not start one morning.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..db import db
from ..engine.health import AlertState, linear_slope

NO_START_V = 12.0
MIN_DAYS = 3


async def daily_resting_voltage(vehicle_id: str, days: int = 14) -> list[dict]:
    bucket = db.bucket("1 day")
    rows = await db.pool.fetch(
        f"""SELECT {bucket} AS day, avg(battery_v) AS avg_v, min(battery_v) AS min_v, count(*) AS n
            FROM telemetry
            WHERE vehicle_id = $1 AND NOT ignition AND battery_v IS NOT NULL
              AND time > now() - make_interval(days => $2)
            GROUP BY day ORDER BY day""",
        vehicle_id, days)
    return [{"day": r["day"].date().isoformat(), "avg_v": round(r["avg_v"], 3),
             "min_v": round(r["min_v"], 3), "samples": r["n"]} for r in rows if r["n"] >= 3]


def evaluate_trend(rows: list[dict]) -> AlertState | None:
    """Return an alert if the trend predicts a no-start within 21 days, else None."""
    if len(rows) < MIN_DAYS:
        return None
    points = [(datetime.fromisoformat(r["day"]).replace(tzinfo=timezone.utc).timestamp() / 86400, r["avg_v"])
              for r in rows]
    slope = linear_slope(points)                    # volts per day
    last = rows[-1]["avg_v"]
    if slope > -0.015 or last <= NO_START_V:
        return None
    eta_days = (NO_START_V - last) / slope
    if not 0 < eta_days <= 21:
        return None
    first = rows[0]["avg_v"]
    msg = (f"Resting battery voltage went from {first:.2f} V to {last:.2f} V over {len(rows)} days "
           f"({slope:+.3f} V/day). At this rate it falls below {NO_START_V:.1f} V - where the engine "
           f"may fail to start - in about {eta_days:.0f} days. Get the battery tested soon.")
    return AlertState(code="battery_trend", signal="battery_v", kind="trend",
                      severity="warning" if eta_days < 3 else "watch",
                      title="Battery is weakening", message=msg, value=last,
                      eta_seconds=eta_days * 86400)
