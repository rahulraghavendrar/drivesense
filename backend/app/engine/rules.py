"""Owner-defined alert rules, set from the website's Alert settings page.

Health alerts (engine, battery, tyres) are always on, for safety. On top of that the
owner chooses what else should trigger an alert:

  speed      the car goes faster than X km/h (for 3 s; one alert per episode)
  low_fuel   fuel drops below X %
  night_use  the car is started during night hours the owner sets (e.g. 23:00-05:00)
  harsh      every harsh brake / acceleration / turn (e.g. someone else is driving)
  geofence   the car enters or leaves one of the owner's geofences
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SPEED_SUSTAIN_S = 3.0
SPEED_REARM_MARGIN = 5.0     # km/h below the limit before the next speed alert can fire
FUEL_REARM_MARGIN = 10.0     # % above the threshold before the next low-fuel alert can fire


@dataclass
class AlertRules:
    speed_alert_kph: float | None = None
    low_fuel_pct: float = 15.0
    night_start: str | None = None     # "HH:MM"
    night_end: str | None = None
    timezone: str = "UTC"
    harsh_driving: bool = False
    geofence: bool = True

    @classmethod
    def from_row(cls, row) -> "AlertRules":
        if row is None:
            return cls()
        return cls(row["speed_alert_kph"], row["low_fuel_pct"], row["night_start"], row["night_end"],
                   row["timezone"], row["harsh_driving"], row["geofence"])

    def to_dict(self) -> dict:
        return {"speed_alert_kph": self.speed_alert_kph, "low_fuel_pct": self.low_fuel_pct,
                "night_start": self.night_start, "night_end": self.night_end, "timezone": self.timezone,
                "harsh_driving": self.harsh_driving, "geofence": self.geofence}


@dataclass
class RuleHit:
    kind: str          # speed | fuel | night
    title: str
    body: str


def _parse_hhmm(s: str | None) -> time | None:
    if not s:
        return None
    h, m = s.split(":")
    return time(int(h), int(m))


def in_window(t: time, start: time, end: time) -> bool:
    """True if t is inside [start, end); handles windows that cross midnight (23:00-05:00)."""
    if start <= end:
        return start <= t < end
    return t >= start or t < end


class RuleTracker:
    def __init__(self, rules: AlertRules | None = None) -> None:
        self.rules = rules or AlertRules()
        self.speed_run = 0.0
        self.speed_fired = False
        self.fuel_fired = False
        self.was_running: bool | None = None
        self.last_ts = None

    def set_rules(self, rules: AlertRules) -> None:
        self.rules = rules
        self.speed_fired = self.fuel_fired = False
        self.speed_run = 0.0

    def _tz(self):
        try:
            return ZoneInfo(self.rules.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("UTC")

    def update(self, r) -> list[RuleHit]:
        hits: list[RuleHit] = []
        rules = self.rules
        dt = (r.ts - self.last_ts).total_seconds() if self.last_ts else 1.0
        dt = dt if 0 < dt <= 5 else 1.0
        self.last_ts = r.ts

        # speed alert
        limit = rules.speed_alert_kph
        if limit and r.ignition and r.speed_kph > limit:
            self.speed_run += dt
            if self.speed_run >= SPEED_SUSTAIN_S and not self.speed_fired:
                self.speed_fired = True
                hits.append(RuleHit("speed", f"Speed alert: {r.speed_kph:.0f} km/h",
                                    f"The car is going faster than your {limit:.0f} km/h alert."))
        elif not limit or r.speed_kph < limit - SPEED_REARM_MARGIN:
            self.speed_run, self.speed_fired = 0.0, False

        # low fuel
        if r.fuel_pct is not None:
            if r.fuel_pct < rules.low_fuel_pct and not self.fuel_fired:
                self.fuel_fired = True
                hits.append(RuleHit("fuel", f"Low fuel: {r.fuel_pct:.0f}%",
                                    f"Fuel is below your {rules.low_fuel_pct:.0f}% alert. Plan a refuel."))
            elif r.fuel_pct > rules.low_fuel_pct + FUEL_REARM_MARGIN:
                self.fuel_fired = False

        # night-time use: fires once, when the ignition turns on inside the window
        start, end = _parse_hhmm(rules.night_start), _parse_hhmm(rules.night_end)
        started = r.ignition and self.was_running is False
        if started and start and end:
            local = r.ts.astimezone(self._tz())
            if in_window(local.time(), start, end):
                hits.append(RuleHit("night", "Car started at night",
                                    f"The car was started at {local:%H:%M}, inside your night-use window "
                                    f"({rules.night_start}-{rules.night_end})."))
        self.was_running = r.ignition
        return hits
