"""Driving-behaviour event detection from a 1 Hz GPS/speed stream.

Seven event types (same set used by commercial connected-car apps):

  HA  Harsh Acceleration           longitudinal accel  >= +3.0 m/s^2  (~0.31 g)
  HB  Harsh Braking                longitudinal accel  <= -3.5 m/s^2  (~0.36 g)
  HT  Harsh Turn                   lateral accel       >= 4.0 m/s^2   (~0.41 g)
  OSL Over Speed Limit             > limit * 1.1 + 2 km/h for >= 5 s
  ABT Accelerating Before Turn     accelerating (>= 1.2 m/s^2) in the 4 s before a turn, entering at >= 20 km/h
  BAT Braking After Turn           braking (<= -2.0 m/s^2) inside a turn or within 3 s after it
  FAB Frequent Accel & Braking     >= 6 switches between accelerating and braking within 60 s

Only two physics formulas are needed:
  longitudinal acceleration  a = dv / dt
  lateral acceleration       a = v * yaw_rate      (yaw_rate = change of heading per second, in rad/s)
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime

HA_THRESHOLD = 3.0
HB_THRESHOLD = -3.5
HT_THRESHOLD = 4.0
HT_MIN_SPEED = 5.0           # m/s - ignore heading noise when crawling
OSL_FACTOR, OSL_MARGIN_KPH, OSL_MIN_SECONDS = 1.10, 2.0, 5.0
TURN_YAW_START, TURN_YAW_END, TURN_MIN_ANGLE = 8.0, 4.0, 45.0   # deg/s, deg/s, deg
ABT_LOOKBACK_S, ABT_ACCEL, ABT_MIN_KPH = 4.0, 1.2, 20.0
BAT_WINDOW_S, BAT_DECEL = 3.0, -2.0
FAB_WINDOW_S, FAB_MIN_SWITCHES, FAB_LEVEL = 60.0, 6, 1.5
COOLDOWN_S = {"HA": 4.0, "HB": 4.0, "HT": 4.0, "FAB": 60.0}


@dataclass
class DrivingEvent:
    type: str
    time: datetime
    lat: float
    lon: float
    speed_kph: float
    value: float          # the measurement that triggered it (m/s^2, km/h over limit, switches ...)
    detail: str

    def to_dict(self) -> dict:
        return {"type": self.type, "time": self.time.isoformat(), "lat": self.lat, "lon": self.lon,
                "speed_kph": round(self.speed_kph, 1), "value": round(self.value, 2), "detail": self.detail}


@dataclass
class _Sample:
    t: float
    time: datetime
    v: float              # m/s
    kph: float
    heading: float
    lat: float
    lon: float
    a_long: float
    yaw: float            # deg/s
    a_lat: float


def _heading_delta(h_new: float, h_old: float) -> float:
    """Signed smallest angle from h_old to h_new, in degrees (-180, 180]."""
    return ((h_new - h_old + 540.0) % 360.0) - 180.0


class EventDetector:
    """Stateful detector - feed it one reading at a time, in time order."""

    def __init__(self) -> None:
        self.history: deque[_Sample] = deque(maxlen=90)
        self.last_fire: dict[str, float] = {}
        # over-speed episode
        self.osl_seconds = 0.0
        self.osl_peak = 0.0
        self.osl_fired = False
        # turn tracking
        self.in_turn = False
        self.turn_start: _Sample | None = None
        self.turn_angle = 0.0
        self.turn_min_accel = 0.0
        self.turn_calm = 0
        self.turn_abt = False
        self.turn_abt_value = 0.0
        self.turn_entry_kph = 0.0
        self.bat_watch_until: float | None = None
        # accel/brake switching
        self.fab_state = 0
        self.fab_switches: deque[float] = deque()

    # ------------------------------------------------------------------ public
    def update(self, time: datetime, lat: float, lon: float, speed_kph: float,
               heading: float, speed_limit_kph: float | None) -> list[DrivingEvent]:
        t = time.timestamp()
        v = max(speed_kph, 0.0) / 3.6
        prev = self.history[-1] if self.history else None
        a_long = yaw = a_lat = dh = 0.0
        dt = 1.0
        if prev is not None:
            dt = t - prev.t
            if 0 < dt <= 5:
                a_long = (v - prev.v) / dt
                dh = _heading_delta(heading, prev.heading)
                if v > 1.5 and prev.v > 1.5:
                    yaw = dh / dt
                    a_lat = abs(((v + prev.v) / 2) * math.radians(yaw))
                else:
                    dh = 0.0
            else:  # data gap: do not derive accelerations across it
                dt = 1.0
        s = _Sample(t, time, v, speed_kph, heading, lat, lon, a_long, yaw, a_lat)
        self.history.append(s)

        events: list[DrivingEvent] = []
        prev_kph = prev.kph if prev else speed_kph

        # --- HA / HB / HT: instantaneous thresholds with a short cooldown
        if a_long >= HA_THRESHOLD and self._ready("HA", t):
            events.append(self._fire("HA", s, a_long, f"{prev_kph:.0f} -> {speed_kph:.0f} km/h in {dt:.0f} s"))
        if a_long <= HB_THRESHOLD and self._ready("HB", t):
            events.append(self._fire("HB", s, a_long, f"{prev_kph:.0f} -> {speed_kph:.0f} km/h in {dt:.0f} s"))
        if a_lat >= HT_THRESHOLD and v >= HT_MIN_SPEED and self._ready("HT", t):
            events.append(self._fire("HT", s, a_lat, f"cornering at {speed_kph:.0f} km/h ({a_lat / 9.81:.2f} g sideways)"))

        # --- OSL: sustained over-speed, one event per episode
        if speed_limit_kph:
            if speed_kph > speed_limit_kph * OSL_FACTOR + OSL_MARGIN_KPH:
                self.osl_seconds += dt
                self.osl_peak = max(self.osl_peak, speed_kph - speed_limit_kph)
                if self.osl_seconds >= OSL_MIN_SECONDS and not self.osl_fired:
                    self.osl_fired = True
                    events.append(self._fire(
                        "OSL", s, self.osl_peak,
                        f"{speed_kph:.0f} km/h in a {speed_limit_kph:.0f} km/h zone"))
            elif speed_kph <= speed_limit_kph * 1.03:
                self.osl_seconds, self.osl_peak, self.osl_fired = 0.0, 0.0, False

        # --- turns -> ABT / BAT
        events.extend(self._track_turn(s, dh))

        # --- FAB: count flips between "accelerating" and "braking"
        state = 1 if a_long >= FAB_LEVEL else (-1 if a_long <= -FAB_LEVEL else 0)
        if state != 0 and state != self.fab_state:
            if self.fab_state != 0:
                self.fab_switches.append(t)
            self.fab_state = state
        while self.fab_switches and t - self.fab_switches[0] > FAB_WINDOW_S:
            self.fab_switches.popleft()
        if len(self.fab_switches) >= FAB_MIN_SWITCHES and self._ready("FAB", t):
            n = len(self.fab_switches)
            self.fab_switches.clear()
            events.append(self._fire("FAB", s, n, f"{n} accelerate/brake switches within a minute"))

        return events

    # ----------------------------------------------------------------- private
    def _track_turn(self, s: _Sample, dh: float) -> list[DrivingEvent]:
        out: list[DrivingEvent] = []
        if not self.in_turn and abs(s.yaw) >= TURN_YAW_START:
            self.in_turn = True
            self.turn_start = s
            self.turn_angle = dh
            self.turn_min_accel = s.a_long
            self.turn_calm = 0
            before = [h for h in self.history if s.t - ABT_LOOKBACK_S <= h.t < s.t]
            peak_accel = max((h.a_long for h in before), default=0.0)
            entry_kph = before[-1].kph if before else s.kph
            self.turn_abt = peak_accel >= ABT_ACCEL and entry_kph >= ABT_MIN_KPH
            self.turn_abt_value = peak_accel
            self.turn_entry_kph = entry_kph
        elif self.in_turn:
            self.turn_angle += dh
            self.turn_min_accel = min(self.turn_min_accel, s.a_long)
            self.turn_calm = self.turn_calm + 1 if abs(s.yaw) < TURN_YAW_END else 0
            if self.turn_calm >= 2 or s.t - self.turn_start.t > 15:
                self.in_turn = False
                if abs(self.turn_angle) >= TURN_MIN_ANGLE:
                    angle = abs(self.turn_angle)
                    if self.turn_abt:
                        out.append(self._fire(
                            "ABT", self.turn_start, self.turn_abt_value,
                            f"sped up to {self.turn_entry_kph:.0f} km/h right before a {angle:.0f} deg turn"))
                    if self.turn_min_accel <= BAT_DECEL:
                        out.append(self._fire("BAT", s, self.turn_min_accel,
                                              f"braked at {self.turn_min_accel:.1f} m/s^2 while turning"))
                    else:
                        self.bat_watch_until = s.t + BAT_WINDOW_S
        if self.bat_watch_until is not None and not self.in_turn:
            if s.t > self.bat_watch_until:
                self.bat_watch_until = None
            elif s.a_long <= BAT_DECEL:
                self.bat_watch_until = None
                out.append(self._fire("BAT", s, s.a_long, f"braked at {s.a_long:.1f} m/s^2 just after a turn"))
        return out

    def _ready(self, etype: str, t: float) -> bool:
        return t - self.last_fire.get(etype, -1e9) >= COOLDOWN_S.get(etype, 0.0)

    def _fire(self, etype: str, s: _Sample, value: float, detail: str) -> DrivingEvent:
        self.last_fire[etype] = s.t
        return DrivingEvent(etype, s.time, s.lat, s.lon, s.kph, value, detail)
