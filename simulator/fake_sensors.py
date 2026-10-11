#!/usr/bin/env python3
"""
DriveSense fake sensor feeder - pretends to be the telematics hardware of YOUR car.

We have no real vehicles or sensors, so this script generates realistic, randomised
sensor data and sends it to the backend exactly the way a real in-car telematics unit
(TCU) would: an HTTP POST to /api/ingest once per second. The backend cannot tell the
difference - replace this script with a real device and nothing else changes.

What is simulated (all random, but physically plausible):
  * one personal car; every trip is driven in a random style (calm / normal / aggressive),
    so scores, fuel economy and coaching vary from trip to trip
  * real road routes around Dearborn, Michigan (OpenStreetMap routing, cached), or a
    synthetic street grid when offline
  * a driver model: speed limits, curves, traffic lights, late "surprise" stops,
    tailgating surges, speeding up before turns ...
  * sensors: GPS, speed, heading, RPM, fuel, odometer, battery voltage, coolant
    temperature, 4 tyre pressures, headlights, door locks, alarm
  * random faults: coolant leak, slow puncture, alternator fault, weakening battery
  * remote commands from the app (lock/unlock, demo buttons) via the ingest response

Only the Python standard library is used - no pip install needed.

Usage
  python fake_sensors.py                         # live mode (Ctrl+C to stop)
  python fake_sensors.py --seed-days 7           # back-fill 7 days of history, then go live
  python fake_sensors.py --reset --seed-days 7   # wipe the database first
  python fake_sensors.py --offline-routes        # never call the routing service
  python fake_sensors.py --driver Sam --model Escape --plate "MI 7ABC12"   # describe your car
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

EARTH_R = 6_371_000.0
LOCAL_UTC_OFFSET_H = -4            # Dearborn (EDT); only used to schedule history trips at realistic hours
STEP_M = 2.0                       # resolution of the pre-computed route profile
EMERGENCY_BRAKE = 5.0              # m/s^2 - anyone brakes this hard when surprised
MAX_LATERAL = 8.0                  # m/s^2 (~0.8 g) - tyre grip limit when cornering
ROUTE_CACHE = Path(__file__).with_name(".route_cache.json")
OSRM_URL = "https://router.project-osrm.org/route/v1/driving/{a_lon},{a_lat};{b_lon},{b_lat}" \
           "?overview=full&geometries=geojson&annotations=speed"

PLACES = [
    ("Ford World Headquarters", 42.3157, -83.2104),
    ("Fairlane Town Center", 42.3191, -83.2169),
    ("University of Michigan-Dearborn", 42.3172, -83.2307),
    ("The Henry Ford Museum", 42.3034, -83.2340),
    ("Dearborn Transit Center", 42.3068, -83.2386),
    ("West Dearborn Downtown", 42.3224, -83.2453),
    ("Ford Rouge Complex", 42.3005, -83.1625),
    ("East Dearborn (Warren Ave)", 42.3435, -83.1755),
]

CAR_ID = "my-car"
# Typical tank size (L) and base fuel use (L/100 km) per model; anything else uses the default.
MODEL_SPECS = {
    "Ranger": (80, 7.5), "Everest": (80, 8.0), "Transit": (80, 9.0), "F-150": (98, 10.0),
    "Bronco": (64, 9.0), "Escape": (53, 6.0), "Mustang": (61, 9.5), "Explorer": (68, 8.5),
}
# How often the owner drives in each style (rolled per trip).
STYLE_MIX = (("calm", 0.25), ("normal", 0.55), ("aggressive", 0.20))

# How each driver type behaves. accel = range of throttle (m/s^2); brake_cap = normal hardest
# braking; plan_brake = how hard they plan to brake for curves/stops (late brakers plan hard);
# speed = factor applied to the speed limit; lat = sideways g-force they accept in curves;
# abt = chance of speeding up before a turn; surge = per-second chance of a tailgating surge;
# surprise = chance that a stop is noticed late.
PERSONAS = {
    "calm": dict(accel=(1.0, 1.7), brake_cap=2.6, plan_brake=1.4, speed=(0.86, 0.98), lat=2.0,
                 abt=0.0, surge=0.0, surprise=0.03),
    "normal": dict(accel=(1.4, 2.5), brake_cap=3.2, plan_brake=1.9, speed=(0.94, 1.07), lat=2.9,
                   abt=0.08, surge=0.004, surprise=0.08),
    "aggressive": dict(accel=(1.6, 3.2), brake_cap=4.4, plan_brake=3.0, speed=(1.0, 1.18), lat=4.3,
                       abt=0.35, surge=0.02, surprise=0.18),
}
FAULTS = ("coolant_leak", "slow_puncture", "alternator_fault", "weak_battery")


# ============================================================================ geo helpers
def haversine_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def bearing_deg(lat1, lon1, lat2, lon2):
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def wrap180(d):
    return ((d + 540) % 360) - 180


def offset(lat, lon, north_m, east_m):
    return (lat + north_m / 111_320.0, lon + east_m / (111_320.0 * math.cos(math.radians(lat))))


def nearest_place(lat, lon):
    return min(range(len(PLACES)), key=lambda i: haversine_m(lat, lon, PLACES[i][1], PLACES[i][2]))


# ================================================================================ routes
class Route:
    """A drivable polyline plus a pre-computed profile every 2 m:
    smoothed heading, curvature (how sharply the road bends) and speed limit."""

    def __init__(self, pts, limits_kph, source):
        clean, lims = [pts[0]], []
        for i in range(1, len(pts)):
            if haversine_m(*clean[-1], *pts[i]) > 0.5:
                clean.append(pts[i])
                lims.append(limits_kph[min(i - 1, len(limits_kph) - 1)])
        if len(clean) < 2:
            clean.append(offset(*clean[0], 30, 0))
            lims.append(40.0)
        self.pts, self.source = clean, source
        self.cum = [0.0]
        for i in range(1, len(clean)):
            self.cum.append(self.cum[-1] + haversine_m(*clean[i - 1], *clean[i]))
        self.total = self.cum[-1]
        seg_bear = [bearing_deg(*clean[i], *clean[i + 1]) for i in range(len(clean) - 1)]

        n = int(self.total // STEP_M) + 1
        raw, self.limit = [], []
        unwrapped = None
        for j in range(n):
            seg = self._segment(j * STEP_M)
            b = seg_bear[seg]
            unwrapped = b if unwrapped is None else unwrapped + wrap180(b - unwrapped)
            raw.append(unwrapped)
            self.limit.append(lims[seg])
        # moving average over +-10 m turns sharp polyline corners into ~11 m radius arcs
        w = 5
        pre = [0.0]
        for x in raw:
            pre.append(pre[-1] + x)
        self.head = []
        for j in range(n):
            lo, hi = max(0, j - w), min(n, j + w + 1)
            self.head.append((pre[hi] - pre[lo]) / (hi - lo))
        self.kappa = [0.0] * n                         # curvature in rad per metre
        for j in range(1, n - 1):
            self.kappa[j] = math.radians(self.head[j + 1] - self.head[j - 1]) / (2 * STEP_M)
        # turns = stretches with radius < 40 m that change direction by >= 45 degrees
        self.turns = []
        j = 0
        while j < n:
            if abs(self.kappa[j]) > 1 / 40:
                k = j
                while k < n and abs(self.kappa[k]) > 1 / 40:
                    k += 1
                angle = self.head[min(k, n - 1)] - self.head[j]
                if abs(angle) >= 45:
                    self.turns.append((j * STEP_M, k * STEP_M, angle))
                j = k
            j += 1

    def _segment(self, s):
        return min(max(bisect.bisect_right(self.cum, s) - 1, 0), len(self.pts) - 2)

    def idx(self, s):
        return min(max(int(s / STEP_M), 0), len(self.head) - 1)

    def position(self, s):
        s = min(max(s, 0.0), self.total)
        i = self._segment(s)
        seg_len = self.cum[i + 1] - self.cum[i]
        f = (s - self.cum[i]) / seg_len if seg_len else 0
        (la1, lo1), (la2, lo2) = self.pts[i], self.pts[i + 1]
        return la1 + (la2 - la1) * f, lo1 + (lo2 - lo1) * f

    def heading(self, s):
        return self.head[self.idx(s)] % 360


class RouteProvider:
    """Real roads from the public OSRM (OpenStreetMap) router, cached to disk.
    Falls back to a synthetic street grid if the router is unreachable."""

    def __init__(self, offline: bool):
        self.offline = offline
        self.cache = {}
        if ROUTE_CACHE.exists():
            try:
                self.cache = json.loads(ROUTE_CACHE.read_text())
            except ValueError:
                self.cache = {}
        self._warned = False
        self.grid_cache = {}

    def route(self, i: int, j: int, rng):
        """Route from place i to place j (cached per pair, so repeated trips reuse it)."""
        a, b = PLACES[i][1:], PLACES[j][1:]
        key = f"{i}->{j}"
        if key in self.grid_cache:
            return self.grid_cache[key]
        if key in self.cache:
            c = self.cache[key]
            return Route([tuple(p) for p in c["pts"]], c["limits"], "osm")
        if not self.offline:
            try:
                pts, limits = self._osrm(a, b)
                self.cache[key] = {"pts": pts, "limits": limits}
                try:
                    ROUTE_CACHE.write_text(json.dumps(self.cache))
                except OSError:
                    pass
                return Route([tuple(p) for p in pts], limits, "osm")
            except Exception as exc:  # network blocked, rate limited ...
                if not self._warned:
                    print(f"[routes] OpenStreetMap routing unavailable ({exc}); using a synthetic street grid.")
                    self._warned = True
                self.offline = True
        self.grid_cache[key] = self._grid(a, b, rng)
        return self.grid_cache[key]

    @staticmethod
    def _osrm(a, b):
        url = OSRM_URL.format(a_lat=a[0], a_lon=a[1], b_lat=b[0], b_lon=b[1])
        req = urllib.request.Request(url, headers={"User-Agent": "DriveSense-demo/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        time.sleep(1.0)                                   # respect the public demo server
        r = data["routes"][0]
        pts = [[c[1], c[0]] for c in r["geometry"]["coordinates"]]
        speeds = []
        for leg in r.get("legs", []):
            speeds += leg.get("annotation", {}).get("speed", [])
        limits = []
        for i in range(len(pts) - 1):
            kph = (speeds[i] if i < len(speeds) else 12) * 3.6
            limits.append(40.0 if kph < 33 else 56.0 if kph < 50 else 72.0 if kph < 70 else 89.0)
        return pts, limits

    @staticmethod
    def _grid(a, b, rng):
        """Manhattan-style route: two or three straight legs joined by 90 degree turns."""
        mid_lat = a[0] + (b[0] - a[0]) * rng.uniform(0.3, 0.7)
        corners = [a, (mid_lat, a[1]), (mid_lat, b[1]), b] if rng.random() < 0.5 else \
            [a, (a[0], (a[1] + b[1]) / 2), (b[0], (a[1] + b[1]) / 2), b]
        pts, limits = [corners[0]], []
        for p, q in zip(corners, corners[1:]):
            lim = rng.choice([40.0, 56.0, 56.0, 72.0])
            d = haversine_m(*p, *q)
            steps = max(1, int(d // 25))
            for k in range(1, steps + 1):
                pts.append((p[0] + (q[0] - p[0]) * k / steps, p[1] + (q[1] - p[1]) * k / steps))
                limits.append(lim)
        return Route(pts, limits, "grid")


# ================================================================================ driver
class Drive:
    """One trip: a driver persona following a route, second by second."""

    def __init__(self, route: Route, style: str, rng: random.Random):
        self.route, self.style, self.rng = route, style, rng
        p = self.p = PERSONAS[style]
        n = len(route.head)
        # speed factor changes every ~300 m (sometimes a normal driver speeds a bit too)
        blocks = int(route.total // 300) + 1
        factors = []
        for _ in range(blocks):
            f = rng.uniform(*p["speed"])
            if style == "normal" and rng.random() < 0.02:
                f = rng.uniform(1.12, 1.2)
            factors.append(f)
        vmax = []
        for j in range(n):
            v_lim = route.limit[j] / 3.6 * factors[int(j * STEP_M // 300)]
            kap = abs(route.kappa[j])
            v_curve = math.sqrt(p["lat"] / kap) if kap > 1e-4 else 99.0
            vmax.append(min(v_lim, v_curve))
        # backward pass: the fastest speed from which the driver can still slow down in time
        env = [0.0] * n
        for j in range(n - 2, -1, -1):
            env[j] = min(vmax[j], math.sqrt(env[j + 1] ** 2 + 2 * p["plan_brake"] * STEP_M))
        self.env = env
        # traffic lights / stop signs: at many turns plus random mid-block lights
        stops = []
        for (s0, _s1, _ang) in route.turns:
            if rng.random() < 0.45 and s0 > 30:
                stops.append(s0 - 4)
        s = rng.uniform(300, 700)
        while s < route.total - 80:
            if rng.random() < 0.35:
                stops.append(s)
            s += rng.uniform(450, 900)
        self.stops = []
        for s in sorted(stops):
            surprise = rng.random() < p["surprise"]
            self.stops.append({"s": s, "dwell": rng.uniform(4, 28),
                               "visible": rng.uniform(18, 28) if surprise else 150.0})
        self.abt_turns = [(t[0] - 40, t[0] - 15) for t in route.turns if rng.random() < p["abt"]]
        self.s, self.v = 0.0, 0.0
        self.dwell = 0.0
        self.surge = 0.0
        self.done = False

    def step(self, dt: float) -> float:
        """Advance one tick. Returns the longitudinal acceleration used (m/s^2)."""
        rng, p = self.rng, self.p
        if self.dwell > 0:
            self.dwell -= dt
            if self.dwell <= 0:
                self.stops.pop(0)
            return 0.0
        v = self.v
        target = self.env[self.route.idx(self.s + v * dt)]
        stop = self.stops[0] if self.stops else None
        d_stop = stop["s"] - self.s if stop else 1e9
        if stop and d_stop <= stop["visible"]:
            # aim at where we will be after this second, so braking stays on a smooth profile
            target = min(target, math.sqrt(2 * p["plan_brake"] * max(d_stop - 2 - v * dt, 0)))
        if d_stop > 60 and v < 12 and any(a <= self.s <= b for a, b in self.abt_turns):
            target = v + rng.uniform(1.3, 2.2)                 # speeding up into the turn
        if self.surge > 0 and d_stop > 80:
            self.surge -= dt
            target = min(v + 2.0, target + 2.5)               # close the gap, then ease off again
        elif p["surge"] and d_stop > 80 and target >= v - 0.3 and rng.random() < p["surge"]:
            self.surge = rng.uniform(1.5, 2.5)                 # tailgating: speed up, then brake

        if target > v:
            a = min(rng.uniform(*p["accel"]), (target - v) / dt)
        else:
            need = (v - target) / dt
            # only a stop that suddenly appears close ahead justifies an emergency stop;
            # otherwise the driver brakes at their normal maximum (and maybe takes a curve too fast)
            emergency = stop is not None and d_stop <= stop["visible"] and d_stop < 40
            a = -min(need, EMERGENCY_BRAKE if (need > p["brake_cap"] and emergency) else p["brake_cap"])
        v_new = max(0.0, v + a * dt)
        ds = (v + v_new) / 2 * dt
        if stop and self.s + ds >= stop["s"]:
            if v_new <= 2.5:
                ds, v_new = max(0.0, stop["s"] - self.s), 0.0
                self.dwell = stop["dwell"]
            else:
                self.stops.pop(0)                              # light turned green - carry on
        elif stop and d_stop < 6 and v_new < 0.5:
            v_new, self.dwell = 0.0, stop["dwell"]
        self.s = min(self.s + ds, self.route.total)
        if self.route.total - self.s < 6 and v_new < 0.6:
            self.s, v_new, self.done = self.route.total, 0.0, True
        a_real = (v_new - v) / dt
        self.v = v_new
        return a_real


# =============================================================================== vehicle
class Vehicle:
    def __init__(self, spec: dict, rng: random.Random, now: datetime, history_start: datetime):
        self.spec, self.rng = spec, rng
        self.id = spec["id"]
        home = PLACES[spec["home"]]
        self.home = (home[1], home[2])
        self.lat, self.lon = self.home
        self.heading = rng.uniform(0, 360)
        self.odometer = rng.uniform(8_000, 45_000)
        self.fuel = rng.uniform(45, 90)
        self.coolant = 18.0
        self.rest_base = 12.63
        self.surface = 0.0
        self.alt_drop = 0.0
        self.leak_heat = 0.0
        self.tire_base = [35 + rng.uniform(-0.3, 0.3) for _ in range(4)]
        self.tire_leak = [0.0] * 4
        self.tire_warm = 0.0
        self.faults: set[str] = set()
        self.random_faults: set[str] = set()
        self.puncture_tire = 0
        self.doors_locked, self.alarm_on = True, True
        self.headlights_override = None
        self.drive: Drive | None = None
        self.park_until = now + timedelta(seconds=rng.uniform(10, 120))
        self.parked_since = now
        self.pending_style = None
        self.pending_short = True
        self.force_emit = True
        self.last_emit = now - timedelta(hours=1)
        self.history_start = history_start
        self.trip_count = 0

    # ------------------------------------------------------------------ physics
    def _battery_rest_base(self, now: datetime) -> float:
        decline = self.spec.get("battery_decline_v_per_day", 0.0)
        if decline:
            days = (now - self.history_start).total_seconds() / 86400
            return 12.62 - decline * days
        return self.rest_base

    def _update_sensors(self, dt: float, a: float, now: datetime) -> None:
        running = self.drive is not None
        v = self.drive.v if running else 0.0
        if running:
            load = min(1.0, max(a, 0) / 3 + v / 40)
            if "coolant_leak" in self.faults:
                self.leak_heat += 0.12 * dt                    # demo speed: alert within ~3 min of driving
            target = 90 + 3 * load + self.leak_heat
            self.coolant += (target - self.coolant) * (1 - math.exp(-dt / 80))
            self.alt_drop = min(self.alt_drop + 0.012 * dt, 2.6) if "alternator_fault" in self.faults else 0.0
            self.tire_warm += (1.6 - self.tire_warm) * (1 - math.exp(-dt / 600))
        else:
            self.leak_heat = 0.0
            self.coolant += (18 - self.coolant) * (1 - math.exp(-dt / 1500))
            self.surface *= math.exp(-dt / 600)
            self.tire_warm *= math.exp(-dt / 1800)
            if "weak_battery" in self.faults:
                self.rest_base = max(11.2, self.rest_base - 0.004 * dt)   # demo speed: ~0.24 V per minute
        if "slow_puncture" in self.faults:
            i = self.puncture_tire
            self.tire_leak[i] = min(self.tire_leak[i] + (0.025 if running else 0.01) * dt, 23)

    def _reading(self, now: datetime, a: float) -> dict:
        rng = self.rng
        running = self.drive is not None
        v_kph = self.drive.v * 3.6 if running else 0.0
        lat, lon = self.lat, self.lon
        if running:
            lat, lon = offset(lat, lon, rng.gauss(0, 1.5), rng.gauss(0, 1.5))      # GPS noise
        if running:
            battery = 14.15 - self.alt_drop + rng.gauss(0, 0.03)
        else:
            battery = self._battery_rest_base(now) + self.surface + rng.gauss(0, 0.01)
        local_hour = (now.hour + LOCAL_UTC_OFFSET_H) % 24
        lights = self.headlights_override if self.headlights_override is not None else \
            (running and (local_hour >= 19 or local_hour < 7))
        return {
            "vehicle_id": self.id,
            "ts": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "ignition": running,
            "lat": round(lat, 6), "lon": round(lon, 6),
            "heading": round((self.heading + (rng.gauss(0, 0.8) if running else 0)) % 360, 1),
            "speed_kph": round(max(0.0, v_kph + (rng.gauss(0, 0.3) if v_kph > 1 else 0)), 1),
            "speed_limit_kph": self.drive.route.limit[self.drive.route.idx(self.drive.s)] if running else None,
            "rpm": round(780 + v_kph * 27 + max(a, 0) * 250 + rng.gauss(0, 20)) if running else 0,
            "fuel_pct": round(self.fuel, 2),
            "odometer_km": round(self.odometer, 3),
            "battery_v": round(max(battery, 10.5), 2),
            "coolant_c": round(self.coolant + rng.gauss(0, 0.15), 1),
            "tire_fl": self._tire(0), "tire_fr": self._tire(1), "tire_rl": self._tire(2), "tire_rr": self._tire(3),
            "headlights": bool(lights),
            "doors_locked": True if running else self.doors_locked,
            "alarm_on": False if running else self.alarm_on,
        }

    def _tire(self, i: int) -> float:
        return round(max(self.tire_base[i] + self.tire_warm - self.tire_leak[i] + self.rng.gauss(0, 0.05), 5), 2)

    # -------------------------------------------------------------------- trips
    def _start_trip(self, now: datetime, routes: RouteProvider, style: str | None, short: bool | None,
                    fault_rate: float, quiet: bool) -> None:
        here = nearest_place(self.lat, self.lon)
        others = [i for i in range(len(PLACES)) if i != here]
        others.sort(key=lambda i: haversine_m(self.lat, self.lon, PLACES[i][1], PLACES[i][2]))
        if short is None:
            dest = self.rng.choice(others)          # everyday trip: anywhere
        elif short:
            dest = self.rng.choice(others[:3])      # demo "short trip": one of the 3 nearest places
        else:
            dest = self.rng.choice(others[-3:])     # demo "longer route": one of the 3 farthest places
        if style is None:
            roll, acc = self.rng.random(), 0.0
            style = STYLE_MIX[-1][0]
            for name, share in STYLE_MIX:
                acc += share
                if roll < acc:
                    style = name
                    break
        route = routes.route(here, dest, self.rng)
        self.drive = Drive(route, style, self.rng)
        self.heading = route.heading(0)
        self.doors_locked, self.alarm_on, self.headlights_override = True, False, None
        if fault_rate and self.rng.random() < fault_rate:
            f = self.rng.choice(("coolant_leak", "slow_puncture", "alternator_fault"))
            self._add_fault(f, random_fault=True)
        self.trip_count += 1
        if not quiet:
            print(f"[{self.id}] trip #{self.trip_count} started: {style} driver -> {PLACES[dest][0]} "
                  f"({route.total / 1000:.1f} km, {route.source} route)")

    def _end_trip(self, now: datetime, park_range: tuple[float, float], quiet: bool) -> None:
        if not quiet:
            print(f"[{self.id}] trip #{self.trip_count} finished at {PLACES[nearest_place(self.lat, self.lon)][0]}")
        self.drive = None
        self.surface = 0.35                                    # surface charge right after engine off
        self.doors_locked, self.alarm_on = True, True
        self.parked_since = now
        self.park_until = now + timedelta(seconds=self.rng.uniform(*park_range))
        if self.fuel < 18:
            self.fuel = self.rng.uniform(88, 99)               # driver refuels

    def _add_fault(self, fault: str, random_fault: bool = False) -> None:
        self.faults.add(fault)
        if random_fault:
            self.random_faults.add(fault)
        if fault == "slow_puncture":
            self.puncture_tire = self.rng.randrange(4)

    def clear_faults(self) -> None:
        self.faults.clear()
        self.random_faults.clear()
        self.tire_leak = [0.0] * 4
        self.alt_drop = self.leak_heat = 0.0
        self.rest_base = 12.63

    def apply_command(self, cmd: dict, now: datetime) -> None:
        action = cmd.get("action")
        if action == "lock":
            self.doors_locked, self.alarm_on = True, True
        elif action == "unlock":
            self.doors_locked, self.alarm_on = False, False
        elif action in ("alarm_on", "alarm_off"):
            self.alarm_on = action == "alarm_on"
        elif action in ("headlights_on", "headlights_off"):
            self.headlights_override = action == "headlights_on"
        elif action == "start_trip":
            self.pending_style, self.pending_short = cmd.get("style", "normal"), cmd.get("short", True)
            if self.drive is None:
                self.park_until = now
        elif action == "inject_fault" and cmd.get("fault") in FAULTS:
            self._add_fault(cmd["fault"])
        elif action == "clear_faults":
            self.clear_faults()
        print(f"[{self.id}] command received: {json.dumps(cmd)}")
        self.force_emit = True

    # --------------------------------------------------------------------- tick
    def tick(self, now: datetime, dt: float, routes: RouteProvider, *, heartbeat_s: float,
             park_range: tuple[float, float], fault_rate: float, quiet: bool,
             allow_new_trip: bool = True) -> list[dict]:
        """Advance the simulation by dt seconds; returns the readings to upload (0..2)."""
        out = []
        if self.drive is None:
            self._update_sensors(dt, 0.0, now)
            if self.random_faults and (now - self.parked_since).total_seconds() > 120:
                for f in list(self.random_faults):        # the driver had it repaired
                    self.faults.discard(f)
                self.random_faults.clear()
                self.tire_leak = [0.0] * 4
            if allow_new_trip and (now >= self.park_until or self.pending_style):
                style, short = self.pending_style, self.pending_short
                self.pending_style = None
                self._start_trip(now, routes, style, short if style else None, fault_rate, quiet)
                out.append(self._reading(now, 0.0))      # ignition on
                self.last_emit = now
                return out
            every = min(heartbeat_s, 2.0) if self.faults else heartbeat_s
            if self.force_emit or (now - self.last_emit).total_seconds() >= every:
                out.append(self._reading(now, 0.0))      # parked heartbeat
                self.last_emit, self.force_emit = now, False
            return out

        d = self.drive
        s_before = d.s
        a = d.step(dt)
        self.lat, self.lon = d.route.position(d.s)
        # Tyres can only grip ~0.8 g sideways: if the car is too fast for the bend its heading
        # cannot follow the road as quickly (it "runs wide"), which caps the yaw rate.
        max_turn = math.degrees(MAX_LATERAL / max(d.v, 1.0)) * dt
        self.heading = (self.heading + max(-max_turn, min(max_turn, wrap180(d.route.heading(d.s) - self.heading)))) % 360
        ds_km = max(0.0, d.s - s_before) / 1000
        self.odometer += ds_km
        l100 = self.spec["l100"]
        fuel_l = 0.9 / 3600 * dt + ds_km * l100 / 100 * (1 + 0.45 * max(a, 0))
        self.fuel = max(0.0, self.fuel - fuel_l / self.spec["tank_l"] * 100)
        self._update_sensors(dt, a, now)
        out.append(self._reading(now, a))
        self.last_emit = now
        if d.done:
            self._end_trip(now, park_range, quiet)
            out.append(self._reading(now + timedelta(milliseconds=500), 0.0))   # ignition off
        return out


# =================================================================================== API
class Api:
    def __init__(self, base: str, key: str):
        self.base, self.key = base.rstrip("/"), key

    def call(self, method: str, path: str, body=None, timeout: float = 30):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", "X-Device-Key": self.key})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None


def car_spec(args) -> dict:
    tank_l, l100 = MODEL_SPECS.get(args.model, (60, 8.0))
    return dict(id=CAR_ID, model=args.model, driver=args.driver, plate=args.plate, tank_l=tank_l, l100=l100,
                home=0, battery_decline_v_per_day=0.0 if args.no_battery_wear else 0.025)


def register(api: Api, cars: list[Vehicle]) -> None:
    api.call("POST", "/api/vehicles/register", {"vehicles": [
        {"id": v.id, "name": f"{v.spec['driver']}'s {v.spec['model']}", "model": v.spec["model"],
         "plate": v.spec["plate"], "driver": v.spec["driver"], "tank_l": v.spec["tank_l"],
         "home_lat": v.home[0], "home_lon": v.home[1]} for v in cars]})


def resume_state(api: Api, cars: list[Vehicle]) -> None:
    """Continue from where the backend last saw each car (position, odometer, fuel)."""
    try:
        known = {v["id"]: v.get("latest") for v in api.call("GET", "/api/vehicles")}
    except Exception:
        return
    for v in cars:
        st = known.get(v.id)
        if st:
            v.lat, v.lon = st["lat"], st["lon"]
            v.odometer = st.get("odometer_km") or v.odometer
            v.fuel = st.get("fuel_pct") or v.fuel
        # Continue the slowly weakening battery from where the backend last saw it, so a
        # restart does not make the battery look suddenly healthy again.
        decline = v.spec.get("battery_decline_v_per_day", 0.0)
        if decline:
            try:
                days = api.call("GET", f"/api/vehicles/{v.id}/battery-trend").get("days", [])
            except Exception:
                days = []
            if days:
                worn_days = max(0.0, (12.62 - days[-1]["avg_v"]) / decline)
                v.history_start = datetime.now(timezone.utc) - timedelta(days=worn_days)


# ================================================================================ modes
def seed_history(api: Api, cars: list[Vehicle], routes: RouteProvider, days: int, rng: random.Random,
                 end: datetime) -> None:
    start = end - timedelta(days=days)
    print(f"[seed] generating {days} days of history ({start:%Y-%m-%d} -> {end:%Y-%m-%d %H:%M} UTC) ...")
    slots = [((7, 30), (9, 0), 0.9), ((12, 0), (13, 30), 0.5), ((16, 45), (18, 30), 0.9), ((19, 0), (21, 0), 0.35)]
    total = 0
    for veh in cars:
        veh.park_until = end + timedelta(days=1)       # never start trips on its own during seeding
        veh.lat, veh.lon = veh.home
        trip_times = []
        for d in range(days + 1):
            day = (start + timedelta(days=d)).replace(hour=0, minute=0, second=0, microsecond=0)
            for (h1, m1), (h2, m2), prob in slots:
                if rng.random() < prob:
                    local = day + timedelta(hours=h1, minutes=m1) + timedelta(
                        seconds=rng.uniform(0, ((h2 - h1) * 60 + (m2 - m1)) * 60))
                    t = local - timedelta(hours=LOCAL_UTC_OFFSET_H)
                    if start < t < end - timedelta(minutes=25):
                        trip_times.append(t)
        trip_times.sort()
        t, batch, n_trips = start, [], 0
        while t < end:
            if veh.drive is None:
                nxt = trip_times[0] if trip_times else None
                if nxt and nxt <= t + timedelta(seconds=600):
                    dt = max((nxt - t).total_seconds(), 1.0)
                    veh.tick(nxt, dt, routes, heartbeat_s=1e9, park_range=(1e9, 1e9), fault_rate=0, quiet=True,
                             allow_new_trip=False)
                    t = nxt
                    trip_times.pop(0)
                    veh._start_trip(t, routes, None, None, 0, True)
                    batch.append(veh._reading(t, 0.0))
                    n_trips += 1
                else:
                    step = min(600.0, (end - t).total_seconds())   # never step past `end` (no future timestamps)
                    if step <= 0:
                        break
                    t += timedelta(seconds=step)
                    batch += veh.tick(t, step, routes, heartbeat_s=0, park_range=(1e9, 1e9), fault_rate=0,
                                      quiet=True, allow_new_trip=False)
            else:
                t += timedelta(seconds=1)
                batch += veh.tick(t, 1.0, routes, heartbeat_s=1e9, park_range=(1e9, 1e9), fault_rate=0,
                                  quiet=True, allow_new_trip=False)
            if len(batch) >= 1000:
                api.call("POST", "/api/ingest", {"readings": batch}, timeout=120)
                total += len(batch)
                batch = []
        if batch:
            api.call("POST", "/api/ingest", {"readings": batch}, timeout=120)
            total += len(batch)
        print(f"[seed] {veh.id}: {n_trips} trips")
    print(f"[seed] uploaded {total} readings")


def run_live(api: Api, cars: list[Vehicle], routes: RouteProvider, args) -> None:
    print(f"[live] streaming your car to {api.base} every second. Ctrl+C to stop.")
    pending: list[dict] = []
    last_status = time.monotonic()
    sent_ok = 0
    while True:
        tick_start = time.monotonic()
        now = datetime.now(timezone.utc)
        for v in cars:
            pending += v.tick(now, 1.0, routes, heartbeat_s=10, park_range=(args.park_min, args.park_max),
                              fault_rate=args.fault_rate, quiet=False)
        # post every second, even with no readings, so remote commands arrive within ~1 s
        try:
            resp = api.call("POST", "/api/ingest", {"readings": pending[-5000:], "poll": [v.id for v in cars]},
                            timeout=10)
            sent_ok += len(pending)
            pending = []
            by_id = {v.id: v for v in cars}
            for vid, cmds in (resp or {}).get("commands", {}).items():
                for c in cmds:
                    if vid in by_id:
                        by_id[vid].apply_command(c, now)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # store-and-forward: keep readings and retry next second, like a real TCU
            print(f"[live] backend unreachable ({exc}); buffering {len(pending)} readings")
            pending = pending[-5000:]
        if time.monotonic() - last_status > 15:
            state = "driving" if any(v.drive for v in cars) else "parked"
            print(f"[live] {state}, {sent_ok} readings sent")
            last_status = time.monotonic()
        time.sleep(max(0.0, 1.0 - (time.monotonic() - tick_start)))


def main() -> None:
    ap = argparse.ArgumentParser(description="DriveSense fake sensor feeder (no hardware needed)")
    ap.add_argument("--api", default="http://localhost:8000", help="backend base URL")
    ap.add_argument("--key", default="dev-device-key", help="device key (DEVICE_KEY in backend/.env)")
    ap.add_argument("--driver", default="Alex", help="your name, shown in the app")
    ap.add_argument("--model", default="Ranger", help="car model, e.g. Ranger, Escape, Mustang, F-150")
    ap.add_argument("--plate", default="MI 4KX 218", help="number plate shown in the app")
    ap.add_argument("--no-battery-wear", action="store_true",
                    help="keep the 12 V battery healthy (by default it slowly weakens, to demo the prediction)")
    ap.add_argument("--seed-days", type=int, default=0, help="back-fill N days of history before going live")
    ap.add_argument("--population", type=int, default=20000,
                    help="synthetic community drivers added to the leaderboard when seeding")
    ap.add_argument("--reset", action="store_true", help="wipe all backend data first")
    ap.add_argument("--no-live", action="store_true", help="exit after seeding")
    ap.add_argument("--fault-rate", type=float, default=0.03, help="chance of a random fault per trip")
    ap.add_argument("--park-min", type=float, default=120, help="min seconds parked between live trips")
    ap.add_argument("--park-max", type=float, default=360, help="max seconds parked between live trips")
    ap.add_argument("--offline-routes", action="store_true", help="use a synthetic grid instead of real roads")
    ap.add_argument("--seed", type=int, default=7, help="random seed (same seed = same simulation)")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    api = Api(args.api, args.key)
    try:
        api.call("GET", "/api/health", timeout=5)
    except Exception as exc:
        sys.exit(f"Cannot reach the backend at {args.api} ({exc}). Start it first: uvicorn app.main:app --port 8000")

    if args.reset:
        api.call("POST", "/api/demo/reset")
        print("[setup] backend data wiped")
    now = datetime.now(timezone.utc)
    history_start = now - timedelta(days=args.seed_days or 7)
    cars = [Vehicle(car_spec(args), random.Random(rng.random()), now, history_start)]
    routes = RouteProvider(args.offline_routes)
    register(api, cars)
    print(f"[setup] registered {cars[0].spec['driver']}'s {cars[0].spec['model']} ({cars[0].id})")

    if args.seed_days > 0:
        seed_history(api, cars, routes, args.seed_days, rng, now - timedelta(seconds=90))
        if args.population:
            api.call("POST", "/api/demo/population", {"count": args.population})
            print(f"[seed] added {args.population} community drivers to the leaderboard")
        start = datetime.now(timezone.utc)
        for v in cars:
            v.park_until = start + timedelta(seconds=rng.uniform(10, 120))
            v.parked_since = start
    else:
        resume_state(api, cars)
    if args.no_live:
        return
    try:
        run_live(api, cars, routes, args)
    except KeyboardInterrupt:
        print("\n[live] stopped")


if __name__ == "__main__":
    main()
