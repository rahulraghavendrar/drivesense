"""Vehicle-health monitoring with *predictive* early warnings.

For each monitored signal we keep the last ~90 s of values and run three checks:

1. Threshold:  value crossed the warning / critical limit           -> 'warning' / 'critical'
2. Predictive: value has left its healthy band AND a straight-line fit
               (least squares) over the last 60 s says it will reach the
               critical limit within 15 minutes                    -> 'watch' + ETA
3. Recovery:   value back inside the healthy band for 10 samples   -> resolved

Requiring the value to be outside the healthy band before forecasting is what stops
false alarms during normal behaviour (e.g. coolant climbing from 25 C to 90 C after a
cold start is a steep rise, but it is still inside the healthy band).

The slow, multi-day battery trend is handled separately (see services/battery_trend.py)
because it needs days of history from the database rather than seconds in memory.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

SEVERITY_RANK = {"watch": 1, "warning": 2, "critical": 3}


@dataclass(frozen=True)
class SignalRule:
    code: str
    field: str
    label: str
    unit: str
    direction: str                 # 'up' = too high is bad, 'down' = too low is bad
    band: tuple[float, float]      # healthy band
    warn: float
    crit: float
    when: str = "always"           # always | running | parked
    horizon_s: float = 900.0


RULES: list[SignalRule] = [
    SignalRule("coolant", "coolant_c", "Engine coolant temperature", "°C", "up", (70, 102), 105, 110),
    SignalRule("charging", "battery_v", "Charging system voltage", "V", "down", (13.4, 14.9), 13.2, 12.6,
               when="running"),
    SignalRule("battery_rest", "battery_v", "Resting battery voltage", "V", "down", (12.3, 13.0), 12.1, 11.8,
               when="parked"),
    SignalRule("tire_fl", "tire_fl", "Front-left tyre pressure", "psi", "down", (32, 38), 30, 27),
    SignalRule("tire_fr", "tire_fr", "Front-right tyre pressure", "psi", "down", (32, 38), 30, 27),
    SignalRule("tire_rl", "tire_rl", "Rear-left tyre pressure", "psi", "down", (32, 38), 30, 27),
    SignalRule("tire_rr", "tire_rr", "Rear-right tyre pressure", "psi", "down", (32, 38), 30, 27),
]
RULES_BY_CODE = {r.code: r for r in RULES}


@dataclass
class AlertState:
    code: str
    signal: str
    kind: str
    severity: str
    title: str
    message: str
    value: float
    eta_seconds: float | None
    db_id: int | None = None
    opened_at: datetime | None = None

    def to_dict(self) -> dict:
        return {"id": self.db_id, "code": self.code, "signal": self.signal, "kind": self.kind,
                "severity": self.severity, "title": self.title, "message": self.message,
                "value": round(self.value, 2) if self.value is not None else None,
                "eta_seconds": round(self.eta_seconds) if self.eta_seconds is not None else None,
                "opened_at": self.opened_at.isoformat() if self.opened_at else None}


@dataclass
class AlertChange:
    action: str                    # opened | escalated | updated | resolved
    alert: AlertState


def linear_slope(points: list[tuple[float, float]]) -> float:
    """Least-squares slope (units per second) of (t, value) points."""
    n = len(points)
    if n < 2:
        return 0.0
    mt = sum(p[0] for p in points) / n
    mv = sum(p[1] for p in points) / n
    num = sum((t - mt) * (v - mv) for t, v in points)
    den = sum((t - mt) ** 2 for t, _ in points)
    return num / den if den else 0.0


def fmt_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    if seconds >= 86400:
        return f"{seconds / 86400:.1f} days"
    if seconds >= 3600:
        return f"{seconds // 3600} h {seconds % 3600 // 60} min"
    if seconds >= 60:
        return f"{seconds // 60} min {seconds % 60:02d} s"
    return f"{seconds} s"


@dataclass
class _SignalWindow:
    points: deque = field(default_factory=lambda: deque(maxlen=90))
    healthy_streak: int = 0


class HealthMonitor:
    def __init__(self, active: list[AlertState] | None = None) -> None:
        self.windows: dict[str, _SignalWindow] = {r.code: _SignalWindow() for r in RULES}
        self.active: dict[str, AlertState] = {a.code: a for a in (active or [])}
        self.latest: dict[str, dict] = {}

    def update(self, r) -> list[AlertChange]:
        changes: list[AlertChange] = []
        t = r.ts.timestamp()
        for rule in RULES:
            if rule.when == "running" and not r.ignition:
                continue
            if rule.when == "parked" and r.ignition:
                continue
            value = getattr(r, rule.field, None)
            if value is None:
                continue
            win = self.windows[rule.code]
            if win.points and t - win.points[-1][0] > 30:
                win.points.clear()  # stale window after a gap / state change
            win.points.append((t, float(value)))
            changes.extend(self._evaluate(rule, win, t, float(value)))
        return changes

    # ------------------------------------------------------------------------
    def _healthy(self, rule: SignalRule, v: float) -> bool:
        return v <= rule.band[1] if rule.direction == "up" else v >= rule.band[0]

    def _evaluate(self, rule: SignalRule, win: _SignalWindow, t: float, v: float) -> list[AlertChange]:
        up = rule.direction == "up"
        severity: str | None = None
        kind = "threshold"
        eta: float | None = None
        recent = [p for p in win.points if t - p[0] <= 60]
        slope = linear_slope(recent) if len(recent) >= 15 else 0.0

        if (up and v >= rule.crit) or (not up and v <= rule.crit):
            severity = "critical"
        elif (up and v >= rule.warn) or (not up and v <= rule.warn):
            severity = "warning"
        if not self._healthy(rule, v):
            toward_limit = slope > 1e-4 if up else slope < -1e-4
            if toward_limit and severity != "critical":
                eta = (rule.crit - v) / slope
                if not 0 < eta <= rule.horizon_s:
                    eta = None
                elif severity is None:
                    severity, kind = "watch", "predictive"

        self.latest[rule.code] = {"label": rule.label, "value": round(v, 2), "unit": rule.unit,
                                  "status": severity or "ok", "eta_seconds": eta,
                                  "trend_per_min": round(slope * 60, 3)}
        current = self.active.get(rule.code)

        if severity is None:
            if current is None:
                return []
            win.healthy_streak = win.healthy_streak + 1 if self._healthy(rule, v) else 0
            if win.healthy_streak >= 10:
                del self.active[rule.code]
                current.message = f"{rule.label} is back to normal ({v:.1f} {rule.unit})."
                current.value = v
                return [AlertChange("resolved", current)]
            return []

        win.healthy_streak = 0
        msg = self._message(rule, severity, v, slope, eta)
        title = f"{rule.label} {'trending toward limit' if severity == 'watch' else severity}"
        if current is None:
            alert = AlertState(rule.code, rule.field, kind, severity, title, msg, v, eta)
            self.active[rule.code] = alert
            return [AlertChange("opened", alert)]
        if SEVERITY_RANK[severity] > SEVERITY_RANK.get(current.severity, 0):
            current.severity, current.kind, current.title = severity, kind, title
            current.message, current.value, current.eta_seconds = msg, v, eta
            return [AlertChange("escalated", current)]
        # same or lower severity: keep the higher one, refresh numbers
        current.value = v
        if severity == current.severity:
            current.message, current.eta_seconds = msg, eta
        return [AlertChange("updated", current)]

    @staticmethod
    def _message(rule: SignalRule, severity: str, v: float, slope: float, eta: float | None) -> str:
        if severity == "watch" and eta is not None:
            verb = "rising" if rule.direction == "up" else "falling"
            return (f"{rule.label} is {v:.1f} {rule.unit} and {verb} {abs(slope) * 60:.2f} {rule.unit}/min. "
                    f"At this rate it reaches the critical limit of {rule.crit:g} {rule.unit} in about "
                    f"{fmt_duration(eta)}.")
        limit = rule.crit if severity == "critical" else rule.warn
        cmp = "above" if rule.direction == "up" else "below"
        extra = f" Critical limit ({rule.crit:g} {rule.unit}) in about {fmt_duration(eta)}." if eta else ""
        return f"{rule.label} is {v:.1f} {rule.unit}, {cmp} the {severity} limit of {limit:g} {rule.unit}.{extra}"

    def snapshot(self) -> dict:
        return {"signals": self.latest, "alerts": [a.to_dict() for a in self.active.values()]}
