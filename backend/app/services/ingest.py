"""Ingest pipeline - the heart of the backend.

    device --HTTP batch--> validate --> per-vehicle processor (trips, events, health, geofences)
                                   |--> hot layer: latest state + live push (Redis or in-process)
                                   |--> write-behind buffer --COPY--> PostgreSQL telemetry
                                   '--> effects: trips, alerts, notifications, leaderboard

The live path never waits for the disk: telemetry rows are buffered and written to
PostgreSQL in bulk every 0.5 s with COPY, the fastest way to load rows into Postgres.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone

from ..cache import cache
from ..db import db
from ..engine.health import RULES_BY_CODE, AlertChange, AlertState
from ..engine.processor import Effects, Geofence, VehicleProcessor
from ..engine.rules import AlertRules
from ..engine.scoring import EVENT_INFO
from ..engine.trips import CompletedTrip
from ..geo import describe_location
from ..schemas import AlertSettingsIn, Reading, VehicleIn
from . import battery_trend
from .notify import notify

log = logging.getLogger("drivesense.ingest")

TELEMETRY_COLUMNS = ["time", "vehicle_id", "ignition", "lat", "lon", "heading", "speed_kph", "speed_limit_kph",
                     "rpm", "fuel_pct", "odometer_km", "battery_v", "coolant_c", "tire_fl", "tire_fr", "tire_rl",
                     "tire_rr", "headlights", "doors_locked", "alarm_on"]
LIVE_WINDOW_S = 120
HARSH_TYPES = {"HB", "HA", "HT"}          # readings older than this (history back-fill) do not trigger live notifications
ALERT_DB_REFRESH_S = 10
MAX_CLOCK_SKEW_S = 120
DEFAULT_POPULATION = 20000                # same as fake_sensors.py --population (same random seed -> same drivers)


def _row(r: Reading) -> tuple:
    return (r.ts, r.vehicle_id, r.ignition, r.lat, r.lon, r.heading, r.speed_kph, r.speed_limit_kph, r.rpm,
            r.fuel_pct, r.odometer_km, r.battery_v, r.coolant_c, r.tire_fl, r.tire_fr, r.tire_rl, r.tire_rr,
            r.headlights, r.doors_locked, r.alarm_on)


class IngestService:
    def __init__(self) -> None:
        self.vehicles: dict[str, dict] = {}
        self.processors: dict[str, VehicleProcessor] = {}
        self.trend_alerts: dict[str, AlertState] = {}
        self._rows: list[tuple] = []
        self._flush_lock = asyncio.Lock()
        self._alert_written: dict[tuple[str, str], float] = {}
        self.readings_total = 0
        self.last_ingest_at: datetime | None = None
        self.ingest_ms: list[float] = []

    # ------------------------------------------------------------------ setup
    async def load(self) -> None:
        self.vehicles = {r["id"]: dict(r) for r in await db.pool.fetch("SELECT * FROM vehicles")}
        for vid in self.vehicles:
            await self._refresh_avg_economy(vid)
        rows = await db.pool.fetch("SELECT * FROM health_alerts WHERE status = 'active' AND code = 'battery_trend'")
        for r in rows:
            self.trend_alerts[r["vehicle_id"]] = self._alert_from_row(r)
        await self.warm_hot_layer()

    async def warm_hot_layer(self) -> None:
        """Rebuild the hot layer from PostgreSQL after a restart. With Redis this is mostly a
        no-op (Redis kept everything); with REDIS_URL=memory:// it is how the car's last known
        state, its leaderboard score and the community drivers come back."""
        for vid in self.vehicles:
            try:
                if await cache.get_latest(vid) is None:
                    row = await db.pool.fetchrow(
                        f"SELECT {', '.join(TELEMETRY_COLUMNS)} FROM telemetry WHERE vehicle_id = $1 "
                        "ORDER BY time DESC LIMIT 1", vid)
                    if row:
                        r = Reading(**{("ts" if k == "time" else k): round(v, 6) if isinstance(v, float) else v
                                       for k, v in dict(row).items()})     # REAL columns -> no 55.1399994
                        proc = await self._processor(vid)
                        proc.last_ts, proc.last_reading = r.ts, r
                        await cache.set_latest(vid, self.build_state(vid))
                await self._update_leaderboard(vid)
            except Exception:
                log.exception("Could not warm the hot layer for %s", vid)
        if cache.kind == "memory" and self.vehicles:
            models = sorted({v["model"] for v in self.vehicles.values()})
            await cache.seed_population(DEFAULT_POPULATION, models)

    async def register(self, vehicles: list[VehicleIn]) -> None:
        for v in vehicles:
            await db.pool.execute(
                """INSERT INTO vehicles (id, name, model, plate, driver, tank_l, home_lat, home_lon)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                   ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, model = EXCLUDED.model,
                     plate = EXCLUDED.plate, driver = EXCLUDED.driver, tank_l = EXCLUDED.tank_l,
                     home_lat = EXCLUDED.home_lat, home_lon = EXCLUDED.home_lon""",
                v.id, v.name, v.model, v.plate, v.driver, v.tank_l, v.home_lat, v.home_lon)
            has_fence = await db.pool.fetchval("SELECT 1 FROM geofences WHERE vehicle_id = $1 LIMIT 1", v.id)
            if not has_fence and v.home_lat is not None:
                await db.pool.execute(
                    "INSERT INTO geofences (vehicle_id, name, lat, lon, radius_m) VALUES ($1, 'Home', $2, $3, 250)",
                    v.id, v.home_lat, v.home_lon)
            self.vehicles[v.id] = {**v.model_dump(), "avg_kmpl": self.vehicles.get(v.id, {}).get("avg_kmpl", 12.0)}
            await self.reload_geofences(v.id)

    async def reload_geofences(self, vid: str) -> None:
        if vid in self.processors:
            self.processors[vid].geofences.set_fences(await self._load_fences(vid))

    async def _load_fences(self, vid: str) -> list[Geofence]:
        rows = await db.pool.fetch("SELECT id, name, lat, lon, radius_m FROM geofences WHERE vehicle_id = $1", vid)
        return [Geofence(r["id"], r["name"], r["lat"], r["lon"], r["radius_m"]) for r in rows]

    async def _processor(self, vid: str) -> VehicleProcessor:
        proc = self.processors.get(vid)
        if proc is None:
            rows = await db.pool.fetch(
                "SELECT * FROM health_alerts WHERE vehicle_id = $1 AND status = 'active'", vid)
            active = [self._alert_from_row(r) for r in rows if r["code"] in RULES_BY_CODE]
            proc = VehicleProcessor(vid, self.vehicles[vid].get("tank_l") or 50, await self._load_fences(vid), active,
                                    await self.get_rules(vid))
            self.processors[vid] = proc
        return proc

    # ------------------------------------------------------------ alert rules
    async def get_rules(self, vid: str) -> AlertRules:
        row = await db.pool.fetchrow("SELECT * FROM alert_settings WHERE vehicle_id = $1", vid)
        return AlertRules.from_row(row)

    async def save_rules(self, vid: str, s: AlertSettingsIn) -> AlertRules:
        await db.pool.execute(
            """INSERT INTO alert_settings (vehicle_id, speed_alert_kph, low_fuel_pct, night_start, night_end,
                   timezone, harsh_driving, geofence, updated_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8, now())
               ON CONFLICT (vehicle_id) DO UPDATE SET speed_alert_kph = EXCLUDED.speed_alert_kph,
                 low_fuel_pct = EXCLUDED.low_fuel_pct, night_start = EXCLUDED.night_start,
                 night_end = EXCLUDED.night_end, timezone = EXCLUDED.timezone,
                 harsh_driving = EXCLUDED.harsh_driving, geofence = EXCLUDED.geofence, updated_at = now()""",
            vid, s.speed_alert_kph, s.low_fuel_pct, s.night_start, s.night_end, s.timezone, s.harsh_driving,
            s.geofence)
        rules = await self.get_rules(vid)
        if vid in self.processors:
            self.processors[vid].rules.set_rules(rules)   # takes effect on the very next reading
        return rules

    @staticmethod
    def _alert_from_row(r) -> AlertState:
        return AlertState(code=r["code"], signal=r["signal"], kind=r["kind"], severity=r["severity"],
                          title=r["title"], message=r["message"], value=r["value"], eta_seconds=r["eta_seconds"],
                          db_id=r["id"], opened_at=r["opened_at"])

    # ----------------------------------------------------------------- ingest
    async def ingest(self, readings: list[Reading], poll: list[str] | None = None) -> dict:
        started = time.perf_counter()
        now = datetime.now(timezone.utc)
        groups: dict[str, list[Reading]] = defaultdict(list)
        for r in readings:
            groups[r.vehicle_id].append(r)

        accepted = 0
        for vid, rs in groups.items():
            if vid not in self.vehicles:
                await self.register([VehicleIn(id=vid, name=vid, model="Unknown")])
            proc = await self._processor(vid)
            rs.sort(key=lambda x: x.ts)
            async with proc.lock:
                live_last = False
                for r in rs:
                    if proc.last_ts is not None and r.ts <= proc.last_ts:
                        continue  # duplicate or out-of-order reading
                    if (r.ts - now).total_seconds() > MAX_CLOCK_SKEW_S:
                        continue  # device clock is wrong; a future timestamp would block later data
                    live = (now - r.ts).total_seconds() < LIVE_WINDOW_S
                    fx = proc.process(r)
                    self._rows.append(_row(r))
                    accepted += 1
                    live_last = live
                    await self._handle_effects(vid, r, fx, live)
                if proc.last_reading is not None:
                    state = self.build_state(vid)
                    await cache.set_latest(vid, state)
                    if live_last:
                        await cache.publish("telemetry", vid, state)

        if len(self._rows) >= 2000:
            await self.flush()
        commands = await cache.pop_commands(sorted(set(groups) | set(poll or [])))
        self.readings_total += accepted
        self.last_ingest_at = now
        if len(readings) <= 50:   # track live-sized batches only (not history back-fill)
            self.ingest_ms.append((time.perf_counter() - started) * 1000)
            self.ingest_ms = self.ingest_ms[-500:]
        return {"accepted": accepted, "commands": commands}

    def build_state(self, vid: str) -> dict:
        """The 'latest state' document cached in Redis and pushed to dashboards."""
        proc = self.processors[vid]
        r = proc.last_reading
        health = proc.health.snapshot()
        if vid in self.trend_alerts:
            health["alerts"].append(self.trend_alerts[vid].to_dict())
        sev = {a["code"]: a["severity"] for a in health["alerts"]}

        def lamp(key: str, label: str, codes: list[str], extra_on: bool = False) -> dict:
            levels = [sev[c] for c in codes if c in sev]
            level = "critical" if "critical" in levels else "warning" if "warning" in levels else \
                "watch" if levels else ("warning" if extra_on else None)
            return {"key": key, "label": label, "on": level is not None, "level": level}

        lamps = [
            lamp("engine", "Engine temperature", ["coolant"]),
            lamp("battery", "Battery / charging", ["charging", "battery_rest", "battery_trend"]),
            lamp("tpms", "Tyre pressure", ["tire_fl", "tire_fr", "tire_rl", "tire_rr"]),
            lamp("fuel", "Low fuel", [], extra_on=(r.fuel_pct or 100) < 15),
            lamp("door", "Doors unlocked", [], extra_on=(not r.doors_locked and not r.ignition)),
        ]
        issues = sum(1 for l in lamps if l["on"])
        tank = self.vehicles.get(vid, {}).get("tank_l") or 50
        kmpl = self.vehicles.get(vid, {}).get("avg_kmpl") or 12.0
        return {
            "vehicle_id": vid,
            "ts": r.ts.isoformat(),
            "ignition": r.ignition,
            "lat": r.lat, "lon": r.lon, "heading": r.heading,
            "speed_kph": round(r.speed_kph, 1), "speed_limit_kph": r.speed_limit_kph,
            "rpm": r.rpm, "fuel_pct": r.fuel_pct,
            "range_km": round((r.fuel_pct or 0) / 100 * tank * kmpl) if r.fuel_pct is not None else None,
            "odometer_km": round(r.odometer_km, 1) if r.odometer_km is not None else None,
            "battery_v": r.battery_v, "coolant_c": r.coolant_c,
            "tires": {"fl": r.tire_fl, "fr": r.tire_fr, "rl": r.tire_rl, "rr": r.tire_rr},
            "headlights": r.headlights, "doors_locked": r.doors_locked, "alarm_on": r.alarm_on,
            "place": describe_location(r.lat, r.lon),
            "trip": proc.live_trip(),
            "health": health,
            "lamps": lamps,
            "status_text": "Car dashboard parameters are okay." if issues == 0
            else f"{issues} item{'s' if issues > 1 else ''} need{'s' if issues == 1 else ''} attention.",
        }

    # --------------------------------------------------------------- effects
    async def _handle_effects(self, vid: str, r: Reading, fx: Effects, live: bool) -> None:
        rules = self.processors[vid].rules.rules
        if live:
            for e in fx.events:
                await cache.publish("driving_event", vid, {**e.to_dict(), "name": EVENT_INFO[e.type]["name"]})
                if rules.harsh_driving and e.type in HARSH_TYPES:
                    await notify(vid, "driving", "warning", f"{EVENT_INFO[e.type]['name']} detected",
                                 f"{e.detail} near {describe_location(e.lat, e.lon)}.", {"event": e.type}, at=r.ts)
        for trip in fx.trips:
            trip_id = await self._persist_trip(trip)
            await self._update_leaderboard(vid)
            await self._refresh_avg_economy(vid)
            if live:
                await notify(vid, "trip", "info", f"Trip completed - score {trip.score}",
                             f"{trip.distance_km:.2f} km in {trip.duration_s // 60} min "
                             f"{trip.duration_s % 60:02d} s. Tap to see your AI coach's feedback.",
                             {"trip_id": trip_id}, at=r.ts)
                await cache.publish("trip_completed", vid, {"trip_id": trip_id, "score": trip.score})
        for change in fx.alerts:
            await self.persist_alert(vid, change, live, r.ts)
        if live:
            for hit in fx.rule_hits:
                await notify(vid, hit.kind, "warning", hit.title, hit.body, at=r.ts)
            for c in (fx.crossings if rules.geofence else []):
                verb = "entered" if c.direction == "enter" else "left"
                await notify(vid, "geofence", "warning" if c.direction == "exit" else "info",
                             f"Vehicle {verb} '{c.fence.name}'",
                             f"{self.vehicles[vid]['name']} {verb} the {c.fence.name} geofence near "
                             f"{describe_location(r.lat, r.lon)}.",
                             {"geofence_id": c.fence.id, "direction": c.direction}, at=r.ts)

    async def _persist_trip(self, t: CompletedTrip) -> int:
        async with db.pool.acquire() as conn, conn.transaction():
            trip_id = await conn.fetchval(
                """INSERT INTO trips (vehicle_id, start_time, end_time, start_lat, start_lon, end_lat, end_lon,
                       start_place, end_place, distance_km, duration_s, max_speed_kph, avg_speed_kph,
                       fuel_used_l, fuel_economy_kmpl, score, event_counts, route_preview)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18) RETURNING id""",
                t.vehicle_id, t.start_time, t.end_time, t.start_lat, t.start_lon, t.end_lat, t.end_lon,
                describe_location(t.start_lat, t.start_lon), describe_location(t.end_lat, t.end_lon),
                t.distance_km, t.duration_s, t.max_speed_kph, t.avg_speed_kph, t.fuel_used_l,
                t.fuel_economy_kmpl, t.score, t.event_counts, t.route_preview)
            if t.events:
                await conn.executemany(
                    """INSERT INTO driving_events (trip_id, vehicle_id, time, type, lat, lon, speed_kph, value, detail)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
                    [(trip_id, t.vehicle_id, e.time, e.type, e.lat, e.lon, e.speed_kph, e.value, e.detail)
                     for e in t.events])
        return trip_id

    async def _update_leaderboard(self, vid: str) -> None:
        score = await db.pool.fetchval(
            """SELECT sum(score * distance_km) / NULLIF(sum(distance_km), 0) FROM trips
               WHERE vehicle_id = $1 AND end_time > now() - INTERVAL '7 days'""", vid)
        if score is not None:
            await cache.update_driver_score(vid, self.vehicles[vid]["model"], round(float(score), 1))

    async def _refresh_avg_economy(self, vid: str) -> None:
        kmpl = await db.pool.fetchval(
            """SELECT sum(distance_km) / NULLIF(sum(fuel_used_l), 0) FROM
               (SELECT distance_km, fuel_used_l FROM trips WHERE vehicle_id = $1 AND fuel_used_l > 0
                ORDER BY start_time DESC LIMIT 20) t""", vid)
        if kmpl:
            self.vehicles[vid]["avg_kmpl"] = float(kmpl)

    async def persist_alert(self, vid: str, change: AlertChange, live: bool, at: datetime) -> None:
        a = change.alert
        key = (vid, a.code)
        if change.action == "opened":
            row = await db.pool.fetchrow(
                """INSERT INTO health_alerts (vehicle_id, code, signal, kind, severity, title, message, value,
                       eta_seconds, opened_at, updated_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$10)
                   ON CONFLICT (vehicle_id, code) WHERE status = 'active' DO UPDATE SET
                     kind = EXCLUDED.kind, severity = EXCLUDED.severity, title = EXCLUDED.title,
                     message = EXCLUDED.message, value = EXCLUDED.value, eta_seconds = EXCLUDED.eta_seconds,
                     updated_at = EXCLUDED.updated_at
                   RETURNING id, opened_at""",
                vid, a.code, a.signal, a.kind, a.severity, a.title, a.message, a.value, a.eta_seconds, at)
            a.db_id, a.opened_at = row["id"], row["opened_at"]
            self._alert_written[key] = time.monotonic()
            if live:
                await notify(vid, "health", a.severity, a.title, a.message, {"alert_id": a.db_id}, at=at)
        elif change.action in ("escalated", "updated"):
            if change.action == "updated" and time.monotonic() - self._alert_written.get(key, 0) < ALERT_DB_REFRESH_S:
                return
            await db.pool.execute(
                """UPDATE health_alerts SET kind=$2, severity=$3, title=$4, message=$5, value=$6, eta_seconds=$7,
                   updated_at=$8 WHERE id=$1""",
                a.db_id, a.kind, a.severity, a.title, a.message, a.value, a.eta_seconds, at)
            self._alert_written[key] = time.monotonic()
            if change.action == "escalated" and live:
                await notify(vid, "health", a.severity, a.title, a.message, {"alert_id": a.db_id}, at=at)
        elif change.action == "resolved":
            await db.pool.execute(
                "UPDATE health_alerts SET status='resolved', resolved_at=$2, message=$3, updated_at=$2 WHERE id=$1",
                a.db_id, at, a.message)
            self._alert_written.pop(key, None)
            if live:
                await notify(vid, "health", "info", f"Resolved: {a.title}", a.message, {"alert_id": a.db_id}, at=at)

    # ------------------------------------------------------- background jobs
    async def flush(self) -> None:
        async with self._flush_lock:
            if not self._rows:
                return
            rows, self._rows = self._rows, []
            try:
                async with db.pool.acquire() as conn:
                    await conn.copy_records_to_table("telemetry", records=rows, columns=TELEMETRY_COLUMNS)
            except Exception:
                log.exception("Telemetry flush failed; re-queueing %d rows", len(rows))
                self._rows = rows + self._rows

    async def run_flusher(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            await self.flush()

    async def run_housekeeping(self) -> None:
        """Close trips of vehicles that went silent, re-evaluate the multi-day battery trend and,
        on plain PostgreSQL (Supabase), delete telemetry past the retention window once an hour."""
        last_retention = 0.0
        while True:
            await asyncio.sleep(30)
            try:
                now = datetime.now(timezone.utc)
                for vid, proc in list(self.processors.items()):
                    async with proc.lock:
                        for trip in proc.trips.close_if_stale(now):
                            await self._persist_trip(trip)
                            await self._update_leaderboard(vid)
                await self.check_battery_trends()
                if time.monotonic() - last_retention > 3600:
                    last_retention = time.monotonic()
                    await db.apply_retention()
            except Exception:
                log.exception("Housekeeping failed")

    async def check_battery_trends(self) -> None:
        for vid in list(self.vehicles):
            rows = await battery_trend.daily_resting_voltage(vid)
            alert = battery_trend.evaluate_trend(rows)
            current = self.trend_alerts.get(vid)
            now = datetime.now(timezone.utc)
            if alert and current is None:
                self.trend_alerts[vid] = alert
                await self.persist_alert(vid, AlertChange("opened", alert), True, now)
            elif alert and current:
                alert.db_id, alert.opened_at = current.db_id, current.opened_at
                action = "escalated" if alert.severity != current.severity else "updated"
                self.trend_alerts[vid] = alert
                await self.persist_alert(vid, AlertChange(action, alert), True, now)
            elif current and not alert:
                del self.trend_alerts[vid]
                current.message = "Resting battery voltage is stable again."
                await self.persist_alert(vid, AlertChange("resolved", current), True, now)

    def reset(self) -> None:
        self.processors.clear()
        self.trend_alerts.clear()
        self._rows.clear()
        self._alert_written.clear()


ingest_service = IngestService()
