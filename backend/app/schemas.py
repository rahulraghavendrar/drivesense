"""Request/response models (validated by FastAPI + Pydantic)."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class Reading(BaseModel):
    """One sensor snapshot from a vehicle's telematics unit (here: the fake feeder)."""
    vehicle_id: str = Field(max_length=40)
    ts: datetime
    ignition: bool
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    heading: float = Field(default=0, ge=0, lt=360.0001)
    speed_kph: float = Field(ge=0, le=300)
    speed_limit_kph: float | None = Field(default=None, ge=0, le=200)
    rpm: float | None = Field(default=None, ge=0, le=10000)
    fuel_pct: float | None = Field(default=None, ge=0, le=100)
    odometer_km: float | None = Field(default=None, ge=0)
    battery_v: float | None = Field(default=None, ge=0, le=20)
    coolant_c: float | None = Field(default=None, ge=-50, le=200)
    tire_fl: float | None = Field(default=None, ge=0, le=100)
    tire_fr: float | None = Field(default=None, ge=0, le=100)
    tire_rl: float | None = Field(default=None, ge=0, le=100)
    tire_rr: float | None = Field(default=None, ge=0, le=100)
    headlights: bool = False
    doors_locked: bool = True
    alarm_on: bool = False


class IngestBatch(BaseModel):
    readings: list[Reading] = Field(default_factory=list, max_length=5000)
    # vehicle ids that want their queued commands even if they sent no reading this time
    poll: list[str] = Field(default_factory=list, max_length=100)


class VehicleIn(BaseModel):
    id: str = Field(max_length=40)
    name: str
    model: str
    plate: str | None = None
    driver: str | None = None
    tank_l: float = 50
    home_lat: float | None = None
    home_lon: float | None = None


class RegisterBatch(BaseModel):
    vehicles: list[VehicleIn]


class GeofenceIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    radius_m: float = Field(ge=50, le=20000)


class AlertSettingsIn(BaseModel):
    speed_alert_kph: float | None = Field(default=None, ge=20, le=250)
    low_fuel_pct: float = Field(default=15, ge=5, le=50)
    night_start: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    night_end: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    timezone: str = Field(default="UTC", max_length=64)
    harsh_driving: bool = False
    geofence: bool = True


class TripRename(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class RemoteCommand(BaseModel):
    action: Literal["lock", "unlock", "alarm_on", "alarm_off", "headlights_on", "headlights_off"]


class DemoTrip(BaseModel):
    style: Literal["calm", "normal", "aggressive"] = "normal"
    short: bool = True


class DemoFault(BaseModel):
    fault: Literal["coolant_leak", "slow_puncture", "alternator_fault", "weak_battery"]


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    text: str = Field(max_length=4000)


class CoachChatIn(BaseModel):
    vehicle_id: str
    question: str = Field(min_length=2, max_length=1000)
    history: list[ChatTurn] = Field(default_factory=list, max_length=8)
