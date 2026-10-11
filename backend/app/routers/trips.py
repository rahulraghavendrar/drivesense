"""Trips, driving analysis and leaderboard."""
import csv
import io

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ..cache import cache
from ..db import db
from ..engine.scoring import EVENT_INFO, penalty_breakdown, score_band
from ..schemas import TripRename
from ..services.analytics import driving_analysis
from ..services.ingest import ingest_service

router = APIRouter(prefix="/api", tags=["trips"])

TRIP_LIST_COLUMNS = """id, vehicle_id, name, start_time, end_time, start_place, end_place, distance_km, duration_s,
    max_speed_kph, avg_speed_kph, fuel_used_l, fuel_economy_kmpl, score, event_counts, route_preview"""


def _trip_out(r) -> dict:
    d = dict(r)
    d["band"] = score_band(d["score"])
    d.pop("coach_summary", None)
    return d


@router.get("/vehicles/{vid}/trips")
async def list_trips(vid: str, limit: int = 30, q: str | None = None):
    if q:
        rows = await db.pool.fetch(
            f"""SELECT {TRIP_LIST_COLUMNS} FROM trips WHERE vehicle_id = $1
                AND (name ILIKE $3 OR start_place ILIKE $3 OR end_place ILIKE $3)
                ORDER BY start_time DESC LIMIT $2""", vid, min(limit, 200), f"%{q}%")
    else:
        rows = await db.pool.fetch(
            f"SELECT {TRIP_LIST_COLUMNS} FROM trips WHERE vehicle_id = $1 ORDER BY start_time DESC LIMIT $2",
            vid, min(limit, 200))
    return [_trip_out(r) for r in rows]


@router.get("/trips/{tid}")
async def trip_detail(tid: int):
    trip = await db.pool.fetchrow("SELECT * FROM trips WHERE id = $1", tid)
    if not trip:
        raise HTTPException(404, "trip not found")
    await ingest_service.flush()   # make sure the last seconds of the trip are on disk
    events = await db.pool.fetch(
        "SELECT id, time, type, lat, lon, speed_kph, value, detail FROM driving_events WHERE trip_id = $1 ORDER BY time",
        tid)
    # Full-resolution route straight from the time-series table (index on vehicle_id, time)
    route = await db.pool.fetch(
        """SELECT time, lat, lon, speed_kph, speed_limit_kph FROM telemetry
           WHERE vehicle_id = $1 AND time BETWEEN $2 AND $3 ORDER BY time""",
        trip["vehicle_id"], trip["start_time"], trip["end_time"])
    step = max(1, len(route) // 900)
    out = _trip_out(trip)
    out["coach_cached"] = trip["coach_summary"] is not None
    out["events"] = [{**dict(e), "name": EVENT_INFO[e["type"]]["name"]} for e in events]
    out["route"] = [[round(r["lat"], 6), round(r["lon"], 6), round(r["speed_kph"], 1),
                     r["speed_limit_kph"] and round(r["speed_limit_kph"])] for r in route[::step]]
    out["breakdown"] = penalty_breakdown(trip["event_counts"], trip["distance_km"])
    return out


@router.patch("/trips/{tid}")
async def rename_trip(tid: int, body: TripRename):
    res = await db.pool.execute("UPDATE trips SET name = $2 WHERE id = $1", tid, body.name)
    if res.endswith("0"):
        raise HTTPException(404, "trip not found")
    return {"id": tid, "name": body.name}


@router.get("/trips/{tid}/export.csv")
async def export_trip(tid: int):
    trip = await db.pool.fetchrow("SELECT vehicle_id, start_time, end_time FROM trips WHERE id = $1", tid)
    if not trip:
        raise HTTPException(404, "trip not found")
    rows = await db.pool.fetch(
        """SELECT time, lat, lon, speed_kph, speed_limit_kph, rpm, fuel_pct, battery_v, coolant_c
           FROM telemetry WHERE vehicle_id = $1 AND time BETWEEN $2 AND $3 ORDER BY time""",
        trip["vehicle_id"], trip["start_time"], trip["end_time"])
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time", "lat", "lon", "speed_kph", "speed_limit_kph", "rpm", "fuel_pct", "battery_v", "coolant_c"])
    for r in rows:
        w.writerow([r["time"].isoformat(), *list(r.values())[1:]])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="trip-{tid}.csv"'})


@router.get("/vehicles/{vid}/analysis")
async def analysis(vid: str, days: int = 7):
    return await driving_analysis(vid, max(1, min(days, 90)))


@router.get("/vehicles/{vid}/leaderboard")
async def leaderboard(vid: str, scope: str = "all"):
    v = ingest_service.vehicles.get(vid)
    if not v:
        raise HTTPException(404, "vehicle not found")
    scope = "model" if scope == "model" else "all"
    lb = await cache.leaderboard(vid, v["model"], scope)

    def label(member: str) -> str:
        if member.startswith("c:"):
            return f"Community driver #{int(member[2:]) + 1}"
        veh = ingest_service.vehicles.get(member, {})
        return f"{veh.get('driver') or member} ({veh.get('model', '')})"

    for item in lb["top"] + lb["around"]:
        item["label"] = label(item["member"])
        item["is_me"] = item["member"] == vid
    lb["model"] = v["model"]
    return lb
