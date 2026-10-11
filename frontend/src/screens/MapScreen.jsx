import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { Plus, Trash2, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Circle, MapContainer, Marker, TileLayer, Tooltip, useMap, useMapEvents } from "react-leaflet";
import { api } from "../api";
import { fmtNum, Sign } from "../components/ui";

export const TILE_URL = "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png";
export const TILE_ATTRIBUTION =
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>';

export function carIcon(heading = 0, moving = false) {
  const fill = moving ? "#0E6B4E" : "#23272B";
  return L.divIcon({
    className: "car-marker",
    iconSize: [34, 34],
    iconAnchor: [17, 17],
    html: `<svg width="34" height="34" viewBox="0 0 34 34" style="transform: rotate(${heading}deg)">
             <circle cx="17" cy="17" r="15" fill="white" stroke="${fill}" stroke-width="2"/>
             <path d="M17 6 L25 26 L17 21 L9 26 Z" fill="${fill}"/></svg>`,
  });
}

function FollowCar({ position, follow }) {
  const map = useMap();
  useEffect(() => {
    if (follow && position) map.panTo(position, { animate: true });
  }, [map, position, follow]);
  return null;
}

function ClickToPlace({ active, onPick }) {
  useMapEvents({ click: (e) => active && onPick([e.latlng.lat, e.latlng.lng]) });
  return null;
}

export default function MapScreen({ vehicle, state }) {
  const [fences, setFences] = useState([]);
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState(null);
  const [name, setName] = useState("");
  const [radius, setRadius] = useState(300);
  const [follow, setFollow] = useState(true);

  const load = () => api.geofences(vehicle.id).then(setFences).catch(() => setFences([]));
  useEffect(() => {
    load();
    setAdding(false);
    setDraft(null);
  }, [vehicle.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const position = state ? [state.lat, state.lon] : null;
  const icon = useMemo(() => carIcon(state?.heading || 0, !!state?.ignition), [state?.heading, state?.ignition]);

  const save = async () => {
    await api.addGeofence(vehicle.id, { name: name.trim() || "My geofence", lat: draft[0], lon: draft[1], radius_m: radius });
    setAdding(false);
    setDraft(null);
    setName("");
    load();
  };

  if (!position) return <div className="panel empty">No location yet for your car.</div>;

  return (
    <div className="map-screen">
      <MapContainer center={position} zoom={15} scrollWheelZoom style={{ height: "100%", minHeight: 300 }}
                    whenReady={(e) => e.target.on("dragstart", () => setFollow(false))}>
        <TileLayer url={TILE_URL} attribution={TILE_ATTRIBUTION} />
        <FollowCar position={position} follow={follow} />
        <ClickToPlace active={adding} onPick={setDraft} />
        {fences.map((f) => (
          <Circle key={f.id} center={[f.lat, f.lon]} radius={f.radius_m}
                  pathOptions={{ color: "#1F5FA8", weight: 2, fillOpacity: 0.08 }}>
            <Tooltip>{f.name} geofence</Tooltip>
          </Circle>
        ))}
        {draft && <Circle center={draft} radius={radius} pathOptions={{ color: "#0E6B4E", dashArray: "6 6", weight: 2 }} />}
        <Marker position={position} icon={icon} />
      </MapContainer>

      <div className="map-sheet">
        {!adding ? (
          <>
            <div className="row between">
              <div className="grow">
                <div className="metric-label">Current location</div>
                <div style={{ fontWeight: 600 }}>{state.place}</div>
                <div className="muted" style={{ fontSize: "var(--fs-xs)" }}>
                  {state.ignition ? `Moving at ${fmtNum(state.speed_kph)} km/h` : "Parked"}
                </div>
              </div>
              {state.ignition && <Sign label="Limit" value={state.speed_limit_kph ? Math.round(state.speed_limit_kph) : "-"} size="sm" />}
            </div>
            <div className="row between" style={{ marginTop: 10 }}>
              <button className="btn" onClick={() => setAdding(true)}><Plus size={16} />Add geofence</button>
              {!follow && <button className="link-btn" onClick={() => setFollow(true)}>Follow car</button>}
            </div>
            {fences.length > 0 && (
              <ul className="list" style={{ marginTop: 8 }}>
                {fences.map((f) => (
                  <li key={f.id} className="row between" style={{ padding: "6px 0", fontSize: "var(--fs-sm)" }}>
                    <span>{f.name} <span className="muted">({fmtNum(f.radius_m)} m)</span></span>
                    <button className="icon-btn" style={{ width: 30, height: 30 }} aria-label={`Delete ${f.name}`}
                            onClick={() => api.deleteGeofence(vehicle.id, f.id).then(load)}>
                      <Trash2 size={14} />
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </>
        ) : (
          <div className="geofence-form">
            <div className="row between">
              <strong>New geofence</strong>
              <button className="icon-btn" style={{ width: 30, height: 30 }} aria-label="Cancel"
                      onClick={() => { setAdding(false); setDraft(null); }}><X size={14} /></button>
            </div>
            <span className="hint">{draft ? "Adjust the radius and give it a name." : "Tap the map to place the centre."}</span>
            <input type="text" placeholder="Name, e.g. Office" value={name} onChange={(e) => setName(e.target.value)} />
            <label className="row" style={{ fontSize: "var(--fs-sm)" }}>
              Radius
              <input type="range" min="100" max="2000" step="50" value={radius} className="grow"
                     onChange={(e) => setRadius(Number(e.target.value))} />
              <span className="num" style={{ width: 54, textAlign: "right" }}>{radius} m</span>
            </label>
            <button className="btn primary block" disabled={!draft} onClick={save}>Save geofence</button>
            <span className="hint">You get a notification whenever the car enters or leaves it.</span>
          </div>
        )}
      </div>
    </div>
  );
}
