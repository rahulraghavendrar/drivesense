"""Unit tests for the pure-Python engine (no database needed):  pytest -q"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.engine.events import EventDetector
from app.engine.health import HealthMonitor, linear_slope
from app.engine.scoring import penalty_breakdown, score_band, trip_score
from app.engine.trips import TripEngine

T0 = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)


def feed(speeds, headings=None, limit=50.0):
    """Feed a 1 Hz speed (+heading) profile into a fresh detector; return the event types."""
    det = EventDetector()
    headings = headings or [90.0] * len(speeds)
    types = []
    for i, (v, h) in enumerate(zip(speeds, headings)):
        types += [e.type for e in det.update(T0 + timedelta(seconds=i), 42.3, -83.2, v, h, limit)]
    return types


# ----------------------------------------------------------------- driving events
def test_smooth_driving_has_no_events():
    speeds = [0, 5, 10, 15, 20, 25, 30, 35, 40, 40, 40, 35, 30, 25, 20, 15, 10, 5, 0]
    assert feed(speeds) == []


def test_harsh_acceleration_and_braking():
    assert "HA" in feed([0, 15, 30, 40, 40])            # +15 km/h in 1 s = 4.2 m/s^2
    assert "HB" in feed([50, 50, 32, 20, 0])           # -18 km/h in 1 s = -5 m/s^2


def test_harsh_turn_from_lateral_acceleration():
    # 35 km/h while heading changes 40 deg/s -> lateral = 9.7 m/s * 0.70 rad/s = 6.8 m/s^2
    speeds = [35] * 6
    headings = [0, 0, 40, 80, 90, 90]
    assert "HT" in feed(speeds, headings)


def test_gentle_turn_is_not_harsh():
    speeds = [18] * 8
    headings = [0, 0, 20, 40, 60, 80, 90, 90]           # 20 deg/s at 5 m/s -> 1.7 m/s^2
    assert "HT" not in feed(speeds, headings)


def test_over_speed_needs_to_be_sustained():
    assert "OSL" not in feed([60, 60, 60, 50, 50], limit=50)   # 3 s only
    assert feed([60] * 8, limit=50).count("OSL") == 1          # one event per episode


def test_accelerating_before_turn():
    speeds = [20, 24, 29, 32, 32, 32, 32, 32, 32]
    headings = [0, 0, 0, 0, 30, 60, 90, 90, 90]
    assert "ABT" in feed(speeds, headings)


def test_braking_after_turn():
    speeds = [20, 20, 20, 20, 20, 10, 10, 10]
    headings = [0, 30, 60, 90, 90, 90, 90, 90]
    assert "BAT" in feed(speeds, headings)


def test_frequent_acceleration_and_braking():
    speeds = []
    for _ in range(5):
        speeds += [30, 37, 30]                         # +/- 7 km/h per second, repeatedly
    assert "FAB" in feed(speeds)


# ------------------------------------------------------------------------ scoring
def test_score_formula():
    assert trip_score({}, 10) == 100
    # 2 harsh brakes (weight 5) on 10 km -> 100 - 2*5*10/10 = 90
    assert trip_score({"HB": 2}, 10) == 90
    # distance floor of 5 km: same events on 1 km count as on 5 km -> 100 - 2*5*2 = 80
    assert trip_score({"HB": 2}, 1) == 80
    assert trip_score({"HB": 50}, 5) == 0
    assert penalty_breakdown({"HB": 2, "OSL": 1}, 10)[0]["type"] == "HB"
    assert [score_band(s) for s in (100, 90, 80, 40)] == ["Excellent", "Good", "Fair", "Okay"]


# ---------------------------------------------------------------- predictive health
def reading(t, coolant=90.0, battery=14.1, ignition=True):
    return SimpleNamespace(ts=T0 + timedelta(seconds=t), ignition=ignition, coolant_c=coolant, battery_v=battery,
                           tire_fl=35.0, tire_fr=35.0, tire_rl=35.0, tire_rr=35.0)


def test_linear_slope():
    assert abs(linear_slope([(0, 1), (1, 3), (2, 5)]) - 2.0) < 1e-9


def test_cold_start_warm_up_is_not_an_alert():
    hm = HealthMonitor()
    changes = []
    for t in range(240):                                 # 20 C -> 92 C in 4 minutes
        changes += hm.update(reading(t, coolant=20 + 0.3 * t))
    assert changes == []


def test_overheating_is_predicted_before_the_limit():
    hm = HealthMonitor()
    opened = None
    for t in range(200):                                 # 95 C rising 0.08 C/s
        value = 95 + 0.08 * t
        for c in hm.update(reading(t, coolant=value)):
            if c.action == "opened" and opened is None:      # record values at the moment it opened
                opened = (value, c.alert.kind, c.alert.severity, c.alert.eta_seconds)
    value, kind, severity, eta = opened
    assert kind == "predictive" and severity == "watch"
    assert value < 105                                   # warned before even the warning limit
    assert 0 < eta < 120


def test_alert_resolves_when_back_to_normal():
    hm = HealthMonitor()
    for t in range(30):
        hm.update(reading(t, battery=12.9))              # charging fault: warning
    actions = []
    for t in range(30, 50):
        actions += [c.action for c in hm.update(reading(t, battery=14.1))]
    assert "resolved" in actions


# ---------------------------------------------------------------------- trip engine
def test_trip_starts_and_ends_with_ignition():
    eng = TripEngine("v1", tank_l=50)
    done = []
    for t in range(301):
        r = SimpleNamespace(ts=T0 + timedelta(seconds=t), ignition=t < 300, lat=42.3 + t * 0.0001, lon=-83.2,
                            speed_kph=40.0, heading=0.0, speed_limit_kph=50.0, odometer_km=1000 + t * 0.011,
                            fuel_pct=50 - t * 0.002)
        done += eng.update(r)[1]
    assert len(done) == 1
    trip = done[0]
    assert abs(trip.distance_km - 3.3) < 0.05            # odometer-based distance
    assert trip.duration_s == 300
    assert trip.score == 100
    assert trip.fuel_economy_kmpl and trip.fuel_economy_kmpl > 0


# ---------------------------------------------------------------- owner alert rules
from app.engine.rules import AlertRules, RuleTracker, in_window  # noqa: E402
from datetime import time as dtime  # noqa: E402


def rr(t, speed=50.0, fuel=60.0, ignition=True, base=T0):
    return SimpleNamespace(ts=base + timedelta(seconds=t), ignition=ignition, speed_kph=speed, fuel_pct=fuel)


def test_speed_alert_fires_once_per_episode():
    tr = RuleTracker(AlertRules(speed_alert_kph=80))
    kinds = []
    for t, v in enumerate([70, 85, 86, 87, 88, 90, 90, 70, 70, 85, 86, 87, 88]):
        kinds += [h.kind for h in tr.update(rr(t, speed=v))]
    assert kinds == ["speed", "speed"]          # two separate episodes, one alert each


def test_low_fuel_alert_and_rearm():
    tr = RuleTracker(AlertRules(low_fuel_pct=20))
    kinds = []
    for t, f in enumerate([25, 19, 18, 17, 95, 19]):     # refuelled in between
        kinds += [h.kind for h in tr.update(rr(t, fuel=f))]
    assert kinds == ["fuel", "fuel"]


def test_night_window_across_midnight():
    assert in_window(dtime(23, 30), dtime(23, 0), dtime(5, 0))
    assert in_window(dtime(2, 0), dtime(23, 0), dtime(5, 0))
    assert not in_window(dtime(12, 0), dtime(23, 0), dtime(5, 0))


def test_night_use_alert_on_ignition():
    rules = AlertRules(night_start="23:00", night_end="05:00", timezone="Asia/Kolkata")
    tr = RuleTracker(rules)
    night_utc = datetime(2026, 10, 1, 19, 0, tzinfo=timezone.utc)      # 00:30 in India
    kinds = []
    for t, ign in enumerate([False, False, True, True]):
        kinds += [h.kind for h in tr.update(rr(t, ignition=ign, base=night_utc))]
    assert kinds == ["night"]
