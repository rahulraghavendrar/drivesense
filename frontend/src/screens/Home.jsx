import { AlertTriangle, BatteryWarning, CircleCheck, CircleDot, DoorOpen, Fuel, Lightbulb, Lock, LockOpen,
  MapPin, Siren, Thermometer } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { MapContainer, Marker, TileLayer, useMap } from "react-leaflet";
import { api } from "../api";
import CarSchematic from "../components/CarSchematic";
import { fmtDuration, fmtNum, ScoreSign, Sign } from "../components/ui";
import WeeklyReport from "../components/WeeklyReport";
import { carIcon, TILE_ATTRIBUTION, TILE_URL } from "./MapScreen";

const LAMP_ICONS = { engine: Thermometer, battery: BatteryWarning, tpms: CircleDot, fuel: Fuel, door: DoorOpen };

function Recenter({ position }) {
  const map = useMap();
  useEffect(() => { map.setView(position, map.getZoom(), { animate: true }); }, [map, position]);
  return null;
}

export default function Home({ car, state, go }) {
  const [pending, setPending] = useState(null);

  // A remote command is "pending" until the car reports the new state back.
  useEffect(() => {
    if (!pending || !state) return;
    const done = {
      lock: state.doors_locked, unlock: !state.doors_locked,
      alarm_on: state.alarm_on, alarm_off: !state.alarm_on,
      headlights_on: state.headlights, headlights_off: !state.headlights,
    }[pending];
    if (done) setPending(null);
  }, [state, pending]);
  useEffect(() => {
    if (!pending) return;
    const t = setTimeout(() => setPending(null), 15000);
    return () => clearTimeout(t);
  }, [pending]);

  const position = useMemo(() => (state ? [state.lat, state.lon] : null), [state?.lat, state?.lon]); // eslint-disable-line
  const icon = useMemo(() => carIcon(state?.heading || 0, !!state?.ignition), [state?.heading, state?.ignition]);

  if (!state) {
    return (
      <div className="panel empty">
        Waiting for the first reading from your car. Start the sensor simulator:<br />
        <code>python fake_sensors.py --reset --seed-days 14</code>
      </div>
    );
  }

  const send = async (action) => {
    setPending(action);
    try { await api.command(car.id, action); } catch { setPending(null); }
  };

  const issues = state.lamps.filter((l) => l.on);
  const worst = issues.some((l) => l.level === "critical") ? "critical" : issues.length ? "attention" : "";
  const fuelSegs = Math.round(((state.fuel_pct ?? 0) / 100) * 12);
  const trip = state.trip;
  const overLimit = state.speed_limit_kph && state.speed_kph > state.speed_limit_kph * 1.1 + 2;

  return (
    <>
      <div className="grid-home">
        <div>
          <button className={`status-line ${worst}`} onClick={() => go({ tab: "health" })}>
            {worst ? <AlertTriangle size={18} /> : <CircleCheck size={18} />}
            <span className="grow">{issues.length ? state.status_text : "All systems normal"}</span>
            <span className="link-btn" style={{ color: "inherit" }}>Health</span>
          </button>

          {state.ignition && trip && (
            <div className="live-strip" aria-live="polite">
              <div className="speed">{Math.round(state.speed_kph)}<small>km/h</small></div>
              <Sign label="Limit" value={state.speed_limit_kph ? Math.round(state.speed_limit_kph) : "-"} over={overLimit} />
              <div className="trip-meta">
                <div>Driving now</div>
                <div><b>{fmtNum(trip.distance_km, 2)}</b> km in <b>{fmtDuration(trip.duration_s)}</b></div>
              </div>
              <ScoreSign score={trip.live_score} />
            </div>
          )}

          <div className="panel" style={{ marginTop: 16 }}>
            <div className="metric-label">Fuel {fmtNum(state.fuel_pct)}%</div>
            <div className={`fuel-bar ${state.fuel_pct < 15 ? "low" : ""}`} aria-hidden="true">
              {Array.from({ length: 12 }, (_, i) => <span key={i} className={i < fuelSegs ? "on" : ""} />)}
            </div>
            <div className="row wrap" style={{ gap: 28, marginTop: 10 }}>
              <Metric label="Range" value={fmtNum(state.range_km)} unit="km" />
              <Metric label="Odometer" value={fmtNum(state.odometer_km)} unit="km" />
              <Metric label="Battery" value={fmtNum(state.battery_v, 1)} unit="V" />
              <Metric label="Coolant" value={fmtNum(state.coolant_c)} unit="°C" />
            </div>
            <div className="lamps" aria-label="Warning lamps">
              {state.lamps.map((l) => {
                const Icon = LAMP_ICONS[l.key] || AlertTriangle;
                return (
                  <span key={l.key} className={`lamp ${l.level || ""}`} title={`${l.label}: ${l.on ? l.level : "off"}`}>
                    <Icon size={18} />
                  </span>
                );
              })}
            </div>
          </div>

          <div className="panel">
            <h2>Remote controls</h2>
            <div className="controls">
              <button className={`control ${state.doors_locked ? "on" : ""}`} disabled={!!pending || state.ignition}
                      onClick={() => send(state.doors_locked ? "unlock" : "lock")}>
                {state.doors_locked ? <Lock size={20} /> : <LockOpen size={20} />}
                {pending === "lock" ? "Locking" : pending === "unlock" ? "Unlocking" : state.doors_locked ? "Locked" : "Unlocked"}
              </button>
              <button className={`control ${state.alarm_on ? "on" : ""}`} disabled={!!pending || state.ignition}
                      onClick={() => send(state.alarm_on ? "alarm_off" : "alarm_on")}>
                <Siren size={20} />
                {pending?.startsWith("alarm") ? "Sending" : state.alarm_on ? "Alarm on" : "Alarm off"}
              </button>
              <button className={`control ${state.headlights ? "on" : ""}`} disabled={!!pending}
                      onClick={() => send(state.headlights ? "headlights_off" : "headlights_on")}>
                <Lightbulb size={20} />
                {pending?.startsWith("headlights") ? "Sending" : state.headlights ? "Lights on" : "Lights off"}
              </button>
            </div>
            <p className="hint" style={{ marginBottom: 0 }}>
              {state.ignition ? "Lock and alarm controls are available when the car is parked."
                : "Commands reach the car within about a second."}
            </p>
          </div>
        </div>

        <div>
          <div className="panel" style={{ display: "flex", justifyContent: "center" }}>
            <CarSchematic state={state} />
          </div>
          <div className="panel">
            <button className="row" onClick={() => go({ tab: "map" })}
                    style={{ border: 0, background: "none", padding: 0, width: "100%", textAlign: "left" }}>
              <MapPin size={18} color="var(--green)" />
              <span className="grow" style={{ fontWeight: 600 }}>{state.place}</span>
              <span className="link-btn">Open map</span>
            </button>
            <div className="mini-map">
              <MapContainer center={position} zoom={14} zoomControl={false} dragging={false} scrollWheelZoom={false}
                            doubleClickZoom={false} touchZoom={false} keyboard={false} attributionControl={false}
                            style={{ height: "100%" }}>
                <TileLayer url={TILE_URL} attribution={TILE_ATTRIBUTION} />
                <Recenter position={position} />
                <Marker position={position} icon={icon} />
              </MapContainer>
            </div>
          </div>
        </div>
      </div>
      <div style={{ marginTop: 16 }}>
        <WeeklyReport car={car} />
      </div>
    </>
  );
}

const Metric = ({ label, value, unit }) => (
  <div>
    <div className="metric-label">{label}</div>
    <div className="metric-value">{value}<small>{unit}</small></div>
  </div>
);
