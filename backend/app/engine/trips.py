"""Trip engine: turns a raw stream of readings into trips.

A trip starts when the ignition turns on and ends when it turns off (or when the
vehicle goes silent for 5 minutes). While a trip is open, every reading is fed to
the EventDetector, so the live score can be shown on the dashboard in real time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..geo import haversine_m
from .events import DrivingEvent, EventDetector
from .scoring import trip_score

MIN_TRIP_KM = 0.1
MAX_GAP_S = 300
PREVIEW_POINTS = 60


@dataclass
class CompletedTrip:
    vehicle_id: str
    start_time: datetime
    end_time: datetime
    start_lat: float
    start_lon: float
    end_lat: float
    end_lon: float
    distance_km: float
    duration_s: int
    max_speed_kph: float
    avg_speed_kph: float
    fuel_used_l: float | None
    fuel_economy_kmpl: float | None
    score: int
    event_counts: dict[str, int]
    events: list[DrivingEvent] = field(default_factory=list)
    route_preview: list[list[float]] = field(default_factory=list)


class TripAccumulator:
    def __init__(self, vehicle_id: str, r, tank_l: float) -> None:
        self.vehicle_id = vehicle_id
        self.tank_l = tank_l
        self.start = r
        self.last = r
        self.detector = EventDetector()
        self.events: list[DrivingEvent] = []
        self.points: list[list[float]] = [[r.lat, r.lon]]
        self.gps_m = 0.0
        self.max_speed = r.speed_kph

    def add(self, r) -> list[DrivingEvent]:
        self.gps_m += haversine_m(self.last.lat, self.last.lon, r.lat, r.lon)
        self.max_speed = max(self.max_speed, r.speed_kph)
        self.points.append([round(r.lat, 6), round(r.lon, 6)])
        new = self.detector.update(r.ts, r.lat, r.lon, r.speed_kph, r.heading, r.speed_limit_kph)
        self.events.extend(new)
        self.last = r
        return new

    # ---------------------------------------------------------------- metrics
    @property
    def distance_km(self) -> float:
        odo = (self.last.odometer_km or 0) - (self.start.odometer_km or 0)
        gps = self.gps_m / 1000
        # Prefer the odometer (it does not suffer from GPS jitter) unless it looks broken.
        if 0 < odo < gps * 2 + 0.5:
            return odo
        return gps

    @property
    def duration_s(self) -> int:
        return int((self.last.ts - self.start.ts).total_seconds())

    def counts(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for e in self.events:
            c[e.type] = c.get(e.type, 0) + 1
        return c

    def live_stats(self) -> dict:
        dist = self.distance_km
        return {
            "started_at": self.start.ts.isoformat(),
            "distance_km": round(dist, 2),
            "duration_s": self.duration_s,
            "max_speed_kph": round(self.max_speed, 1),
            "event_counts": self.counts(),
            "live_score": trip_score(self.counts(), dist),
        }

    def finish(self) -> CompletedTrip | None:
        dist = self.distance_km
        dur = max(self.duration_s, 1)
        if dist < MIN_TRIP_KM:
            return None
        fuel_used = None
        if self.start.fuel_pct is not None and self.last.fuel_pct is not None:
            used = (self.start.fuel_pct - self.last.fuel_pct) / 100.0 * self.tank_l
            fuel_used = used if used > 0.005 else None
        counts = self.counts()
        step = max(1, len(self.points) // PREVIEW_POINTS)
        preview = self.points[::step]
        if preview[-1] != self.points[-1]:
            preview.append(self.points[-1])
        return CompletedTrip(
            vehicle_id=self.vehicle_id,
            start_time=self.start.ts, end_time=self.last.ts,
            start_lat=self.start.lat, start_lon=self.start.lon,
            end_lat=self.last.lat, end_lon=self.last.lon,
            distance_km=round(dist, 2), duration_s=dur,
            max_speed_kph=round(self.max_speed, 1),
            avg_speed_kph=round(dist / (dur / 3600), 2),
            fuel_used_l=round(fuel_used, 3) if fuel_used else None,
            fuel_economy_kmpl=round(dist / fuel_used, 2) if fuel_used else None,
            score=trip_score(counts, dist), event_counts=counts,
            events=list(self.events), route_preview=preview,
        )


class TripEngine:
    def __init__(self, vehicle_id: str, tank_l: float) -> None:
        self.vehicle_id = vehicle_id
        self.tank_l = tank_l
        self.current: TripAccumulator | None = None

    def update(self, r) -> tuple[list[DrivingEvent], list[CompletedTrip]]:
        """Returns (events detected by this reading, trips that just finished)."""
        done: list[CompletedTrip] = []
        events: list[DrivingEvent] = []
        if self.current and (r.ts - self.current.last.ts).total_seconds() > MAX_GAP_S:
            done += self._close()
        if r.ignition:
            if self.current is None:
                self.current = TripAccumulator(self.vehicle_id, r, self.tank_l)
            else:
                events = self.current.add(r)
        elif self.current is not None:
            events = self.current.add(r)
            done += self._close()
        return events, done

    def close_if_stale(self, now: datetime) -> list[CompletedTrip]:
        if self.current and (now - self.current.last.ts).total_seconds() > MAX_GAP_S:
            return self._close()
        return []

    def _close(self) -> list[CompletedTrip]:
        trip = self.current.finish() if self.current else None
        self.current = None
        return [trip] if trip else []
