"""Per-vehicle stream processor. Pure Python, no I/O, so it is easy to unit-test.

Each reading goes through:  trip engine -> event detector -> health monitor -> geofences
and the processor returns *effects* (things that happened). The ingest service
decides what to persist / publish for each effect.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from ..geo import haversine_m
from .events import DrivingEvent
from .health import AlertChange, AlertState, HealthMonitor
from .rules import AlertRules, RuleHit, RuleTracker
from .trips import CompletedTrip, TripEngine


@dataclass
class Geofence:
    id: int
    name: str
    lat: float
    lon: float
    radius_m: float


@dataclass
class GeofenceCrossing:
    fence: Geofence
    direction: str  # enter | exit


@dataclass
class Effects:
    events: list[DrivingEvent] = field(default_factory=list)
    trips: list[CompletedTrip] = field(default_factory=list)
    alerts: list[AlertChange] = field(default_factory=list)
    crossings: list[GeofenceCrossing] = field(default_factory=list)
    rule_hits: list[RuleHit] = field(default_factory=list)


class GeofenceTracker:
    def __init__(self, fences: list[Geofence]) -> None:
        self.fences = {f.id: f for f in fences}
        self.inside: dict[int, bool] = {}

    def set_fences(self, fences: list[Geofence]) -> None:
        self.fences = {f.id: f for f in fences}
        self.inside = {k: v for k, v in self.inside.items() if k in self.fences}

    def update(self, lat: float, lon: float) -> list[GeofenceCrossing]:
        out = []
        for fid, f in self.fences.items():
            now_inside = haversine_m(lat, lon, f.lat, f.lon) <= f.radius_m
            before = self.inside.get(fid)
            self.inside[fid] = now_inside
            if before is not None and before != now_inside:
                out.append(GeofenceCrossing(f, "enter" if now_inside else "exit"))
        return out


class VehicleProcessor:
    def __init__(self, vehicle_id: str, tank_l: float, fences: list[Geofence],
                 active_alerts: list[AlertState], rules: AlertRules | None = None) -> None:
        self.vehicle_id = vehicle_id
        self.trips = TripEngine(vehicle_id, tank_l)
        self.health = HealthMonitor(active_alerts)
        self.geofences = GeofenceTracker(fences)
        self.rules = RuleTracker(rules)
        self.last_ts = None
        self.last_reading = None
        self.lock = asyncio.Lock()

    def process(self, r) -> Effects:
        fx = Effects()
        fx.events, fx.trips = self.trips.update(r)
        fx.alerts = self.health.update(r)
        fx.crossings = self.geofences.update(r.lat, r.lon)
        fx.rule_hits = self.rules.update(r)
        self.last_ts = r.ts
        self.last_reading = r
        return fx

    def live_trip(self) -> dict | None:
        return self.trips.current.live_stats() if self.trips.current else None
