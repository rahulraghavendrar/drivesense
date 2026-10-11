"""Driving analysis over a period (the 'Driving Analysis' tab) - pure SQL aggregates on trips."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..db import db
from ..engine.scoring import EVENT_INFO, EVENT_ORDER, event_level, score_band


async def period_totals(vehicle_id: str, start: datetime, end: datetime) -> dict:
    """Headline numbers for any time window (used to compare this week with last week)."""
    r = await db.pool.fetchrow(
        """SELECT count(*) AS trips, coalesce(sum(distance_km), 0) AS km,
                  sum(score * distance_km) / NULLIF(sum(distance_km), 0) AS score,
                  sum(distance_km) / NULLIF(sum(fuel_used_l), 0) AS kmpl
           FROM trips WHERE vehicle_id = $1 AND start_time > $2 AND start_time <= $3""", vehicle_id, start, end)
    return {"trips": r["trips"], "km": round(float(r["km"]), 1),
            "score": round(float(r["score"])) if r["score"] is not None else None,
            "kmpl": round(float(r["kmpl"]), 2) if r["kmpl"] is not None else None}


async def driving_analysis(vehicle_id: str, days: int = 7) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    agg = await db.pool.fetchrow(
        """SELECT count(*) AS trips, coalesce(sum(distance_km), 0) AS dist, max(max_speed_kph) AS max_speed,
                  coalesce(sum(duration_s), 0) AS dur, sum(fuel_used_l) AS fuel,
                  sum(score * distance_km) / NULLIF(sum(distance_km), 0) AS score
           FROM trips WHERE vehicle_id = $1 AND start_time > $2""", vehicle_id, since)
    ev_rows = await db.pool.fetch(
        """SELECT e.key AS type, sum(e.value::int) AS n
           FROM trips, jsonb_each_text(event_counts) AS e
           WHERE vehicle_id = $1 AND start_time > $2 GROUP BY e.key""", vehicle_id, since)
    daily_rows = await db.pool.fetch(
        """SELECT date_trunc('day', start_time) AS d, sum(distance_km) AS km,
                  sum(score * distance_km) / NULLIF(sum(distance_km), 0) AS score
           FROM trips WHERE vehicle_id = $1 AND start_time > $2 GROUP BY d ORDER BY d""", vehicle_id, since)

    dist = float(agg["dist"] or 0)
    counts = {r["type"]: int(r["n"]) for r in ev_rows}
    by_day = {r["d"].date(): r for r in daily_rows}
    daily = []
    for i in range(days - 1, -1, -1):
        d = (datetime.now(timezone.utc) - timedelta(days=i)).date()
        r = by_day.get(d)
        daily.append({"date": d.isoformat(), "distance_km": round(float(r["km"]), 2) if r else 0.0,
                      "score": round(float(r["score"])) if r and r["score"] is not None else None})
    events = []
    for t in EVENT_ORDER:
        n = counts.get(t, 0)
        info = EVENT_INFO[t]
        events.append({"type": t, "name": info["name"], "count": n,
                       "per_100km": round(n * 100 / dist, 1) if dist else 0.0,
                       "level": event_level(n, dist), "message": info["tip"] if n else info["good"]})
    score = round(float(agg["score"])) if agg["score"] is not None else None
    fuel = float(agg["fuel"] or 0)
    return {
        "period": {"days": days, "from": since.date().isoformat(), "to": datetime.now(timezone.utc).date().isoformat()},
        "stats": {
            "distance_km": round(dist, 2),
            "trips": agg["trips"],
            "max_speed_kph": round(float(agg["max_speed"]), 1) if agg["max_speed"] is not None else 0,
            "avg_speed_kph": round(dist / (agg["dur"] / 3600), 2) if agg["dur"] else 0,
            "travel_time_s": int(agg["dur"]),
            "fuel_economy_kmpl": round(dist / fuel, 2) if fuel > 0 else None,
        },
        "score": score,
        "band": score_band(score),
        "daily": daily,
        "events": events,
    }
