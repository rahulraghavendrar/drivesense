"""App-facing vehicle APIs: live state, remote commands, geofences, notifications, health."""
from fastapi import APIRouter, HTTPException

from ..cache import cache
from ..db import db
from ..schemas import AlertSettingsIn, GeofenceIn, RemoteCommand
from ..services.notify import notify
from ..services import battery_trend
from ..services.ingest import ingest_service

router = APIRouter(prefix="/api", tags=["vehicles"])


def _vehicle_or_404(vid: str) -> dict:
    v = ingest_service.vehicles.get(vid)
    if not v:
        raise HTTPException(404, "vehicle not found")
    return v


@router.get("/vehicles")
async def list_vehicles():
    vids = sorted(ingest_service.vehicles)
    latest = await cache.get_latest_many(vids)        # one Redis round-trip for all registered cars
    return [{"id": vid, "name": v["name"], "model": v["model"], "plate": v.get("plate"),
             "driver": v.get("driver"), "tank_l": v.get("tank_l"), "latest": latest.get(vid)}
            for vid in vids for v in [ingest_service.vehicles[vid]]]


@router.get("/vehicles/{vid}/live")
async def live_state(vid: str):
    _vehicle_or_404(vid)
    return await cache.get_latest(vid) or {}


@router.get("/vehicles/{vid}/recent")
async def recent_telemetry(vid: str, minutes: int = 10):
    """Recent raw telemetry for live charts (served from the hypertable's newest chunk)."""
    _vehicle_or_404(vid)
    minutes = max(1, min(minutes, 120))
    await ingest_service.flush()
    rows = await db.pool.fetch(
        """SELECT time, speed_kph, coolant_c, battery_v, tire_fl, tire_fr, tire_rl, tire_rr, fuel_pct
           FROM telemetry WHERE vehicle_id = $1 AND time > now() - make_interval(mins => $2)
           ORDER BY time""", vid, minutes)
    step = max(1, len(rows) // 600)
    return [{k: (v.isoformat() if k == "time" else round(v, 2) if isinstance(v, float) else v)
             for k, v in r.items()} for r in rows[::step]]


@router.get("/vehicles/{vid}/battery-trend")
async def get_battery_trend(vid: str, days: int = 14):
    _vehicle_or_404(vid)
    rows = await battery_trend.daily_resting_voltage(vid, max(3, min(days, 60)))
    alert = battery_trend.evaluate_trend(rows)
    return {"days": rows, "prediction": alert.to_dict() if alert else None}


@router.get("/vehicles/{vid}/health")
async def health(vid: str):
    _vehicle_or_404(vid)
    latest = await cache.get_latest(vid) or {}
    history = await db.pool.fetch(
        """SELECT id, code, kind, severity, title, message, status, opened_at, resolved_at
           FROM health_alerts WHERE vehicle_id = $1 ORDER BY opened_at DESC LIMIT 20""", vid)
    return {"live": latest.get("health", {"signals": {}, "alerts": []}),
            "history": [dict(r) for r in history]}


@router.post("/vehicles/{vid}/commands")
async def remote_command(vid: str, cmd: RemoteCommand):
    """Queue a remote command; the vehicle picks it up with its next upload."""
    _vehicle_or_404(vid)
    await cache.push_command(vid, {"action": cmd.action})
    return {"queued": cmd.action}


# ---------------------------------------------------------------- alert rules
@router.get("/vehicles/{vid}/alert-settings")
async def get_alert_settings(vid: str):
    _vehicle_or_404(vid)
    return (await ingest_service.get_rules(vid)).to_dict()


@router.put("/vehicles/{vid}/alert-settings")
async def put_alert_settings(vid: str, body: AlertSettingsIn):
    """Save the owner's alert rules; the stream processor uses them from the next reading."""
    _vehicle_or_404(vid)
    if body.timezone:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(body.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise HTTPException(422, f"unknown time zone: {body.timezone}")
    return (await ingest_service.save_rules(vid, body)).to_dict()


@router.post("/vehicles/{vid}/alerts/test")
async def test_alert(vid: str, severity: str = "warning"):
    """Send a test alert through the real pipeline (DB -> Redis Pub/Sub -> WebSocket -> browser)."""
    _vehicle_or_404(vid)
    sev = severity if severity in ("info", "warning", "critical") else "warning"
    return await notify(vid, "test", sev, "Test alert",
                        "If you can see this, alerts reach this browser. Real alerts look the same.")


# ------------------------------------------------------------------ geofences
@router.get("/vehicles/{vid}/geofences")
async def list_geofences(vid: str):
    _vehicle_or_404(vid)
    rows = await db.pool.fetch("SELECT * FROM geofences WHERE vehicle_id = $1 ORDER BY id", vid)
    return [dict(r) for r in rows]


@router.post("/vehicles/{vid}/geofences", status_code=201)
async def create_geofence(vid: str, g: GeofenceIn):
    _vehicle_or_404(vid)
    row = await db.pool.fetchrow(
        "INSERT INTO geofences (vehicle_id, name, lat, lon, radius_m) VALUES ($1,$2,$3,$4,$5) RETURNING *",
        vid, g.name, g.lat, g.lon, g.radius_m)
    await ingest_service.reload_geofences(vid)
    return dict(row)


@router.delete("/vehicles/{vid}/geofences/{gid}", status_code=204)
async def delete_geofence(vid: str, gid: int):
    await db.pool.execute("DELETE FROM geofences WHERE id = $1 AND vehicle_id = $2", gid, vid)
    await ingest_service.reload_geofences(vid)


# -------------------------------------------------------------- notifications
@router.get("/vehicles/{vid}/notifications")
async def list_notifications(vid: str, limit: int = 50):
    rows = await db.pool.fetch(
        "SELECT * FROM notifications WHERE vehicle_id = $1 ORDER BY time DESC LIMIT $2", vid, min(limit, 200))
    unread = await db.pool.fetchval(
        "SELECT count(*) FROM notifications WHERE vehicle_id = $1 AND NOT is_read", vid)
    return {"unread": unread, "items": [dict(r) for r in rows]}


@router.post("/vehicles/{vid}/notifications/read")
async def mark_read(vid: str):
    await db.pool.execute("UPDATE notifications SET is_read = true WHERE vehicle_id = $1 AND NOT is_read", vid)
    return {"ok": True}
