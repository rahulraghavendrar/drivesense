import L from "leaflet";
import { ChevronLeft, Download, Pencil, RefreshCw, Sparkles } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { MapContainer, Marker, Polyline, TileLayer, Tooltip, useMap } from "react-leaflet";
import { api } from "../api";
import { fmtDate, fmtDuration, fmtNum, fmtTime, Markdown, ModelBadge, ScoreSign, Sources } from "../components/ui";
import { TILE_ATTRIBUTION, TILE_URL } from "./MapScreen";

function FitRoute({ points }) {
  const map = useMap();
  useEffect(() => {
    if (points.length > 1) map.fitBounds(points, { padding: [24, 24] });
  }, [map, points]);
  return null;
}

const eventIcon = (code) => L.divIcon({ className: "event-marker", iconSize: [30, 18], iconAnchor: [15, 9], html: code });

/** Split the route into runs that are at/below the limit (green) or above it (yellow). */
function speedRuns(route) {
  const runs = [];
  let cur = null;
  route.forEach(([lat, lon, speed, limit]) => {
    const over = limit ? speed > limit * 1.1 + 2 : false;
    if (!cur || cur.over !== over) {
      if (cur) cur.pts.push([lat, lon]);
      cur = { over, pts: [] };
      runs.push(cur);
    }
    cur.pts.push([lat, lon]);
  });
  return runs;
}

export default function TripDetail({ tripId, onBack }) {
  const [trip, setTrip] = useState(null);
  const [error, setError] = useState(null);
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState("");
  const [coach, setCoach] = useState(null);
  const [coachLoading, setCoachLoading] = useState(false);

  useEffect(() => {
    setTrip(null);
    setCoach(null);
    api.trip(tripId).then((t) => {
      setTrip(t);
      setName(t.name || "");
      if (t.coach_cached) api.coach(tripId).then(setCoach).catch(() => {});
    }).catch((e) => setError(e.message));
  }, [tripId]);

  const runs = useMemo(() => (trip ? speedRuns(trip.route) : []), [trip]);
  const points = useMemo(() => (trip ? trip.route.map((r) => [r[0], r[1]]) : []), [trip]);

  if (error) return <div className="trip-detail-body">{onBack && <button className="back" onClick={onBack}><ChevronLeft size={16} />Trips</button>}<div className="empty">{error}</div></div>;
  if (!trip) return <div className="empty">Loading trip</div>;

  const askCoach = async (refresh = false) => {
    setCoachLoading(true);
    try {
      setCoach(await api.coach(tripId, refresh));
    } catch (e) {
      setCoach({ answer: `The coach could not answer: ${e.message}`, sources: [] });
    } finally {
      setCoachLoading(false);
    }
  };
  const saveName = async () => {
    await api.renameTrip(tripId, name.trim() || "Trip");
    setTrip({ ...trip, name: name.trim() });
    setEditing(false);
  };

  return (
    <div>
      <div style={{ height: 250 }}>
        {points.length > 1 && (
          <MapContainer bounds={points} style={{ height: "100%" }} scrollWheelZoom={false}>
            <TileLayer url={TILE_URL} attribution={TILE_ATTRIBUTION} />
            <FitRoute points={points} />
            {runs.map((r, i) => (
              <Polyline key={i} positions={r.pts}
                        pathOptions={{ color: r.over ? "#D9A800" : "#0E6B4E", weight: 5, opacity: 0.9 }} />
            ))}
            {trip.events.map((e) => (
              <Marker key={e.id} position={[e.lat, e.lon]} icon={eventIcon(e.type)}>
                <Tooltip>{e.name}: {e.detail}</Tooltip>
              </Marker>
            ))}
          </MapContainer>
        )}
      </div>

      <div className="trip-detail-body">
        {onBack && <button className="back" onClick={onBack}><ChevronLeft size={16} />Trips</button>}
        <div className="row between" style={{ alignItems: "flex-start" }}>
          <div className="grow">
            {editing ? (
              <div className="row">
                <input className="grow" value={name} autoFocus maxLength={80} onChange={(e) => setName(e.target.value)}
                       onKeyDown={(e) => e.key === "Enter" && saveName()}
                       style={{ border: "1px solid var(--line)", borderRadius: 6, padding: "6px 8px" }} />
                <button className="btn" onClick={saveName}>Save</button>
              </div>
            ) : (
              <button className="row" onClick={() => setEditing(true)}
                      style={{ border: 0, background: "none", padding: 0, fontWeight: 700, fontSize: "var(--fs-lg)" }}>
                {trip.name || "Add trip name"} <Pencil size={14} className="muted" />
              </button>
            )}
            <div className="muted" style={{ fontSize: "var(--fs-sm)", marginTop: 2 }}>
              {fmtDate(trip.start_time)}, {fmtTime(trip.start_time)}-{fmtTime(trip.end_time)}
            </div>
            <div style={{ fontSize: "var(--fs-sm)", marginTop: 6 }}>
              <div><span style={{ color: "var(--green)" }}>From</span> {trip.start_place}</div>
              <div><span style={{ color: "var(--red)" }}>To</span> {trip.end_place}</div>
            </div>
          </div>
          <ScoreSign score={trip.score} size="lg" />
        </div>

        <div className="section">
          <h3>Trip statistics</h3>
          <div className="stats-grid">
            <Stat label="Distance" value={fmtNum(trip.distance_km, 2)} unit="km" />
            <Stat label="Max speed" value={fmtNum(trip.max_speed_kph)} unit="km/h" />
            <Stat label="Avg speed" value={fmtNum(trip.avg_speed_kph, 1)} unit="km/h" />
            <Stat label="Travel time" value={fmtDuration(trip.duration_s)} />
            <Stat label="Fuel economy" value={fmtNum(trip.fuel_economy_kmpl, 1)} unit="km/l" />
            <Stat label="Fuel used" value={fmtNum(trip.fuel_used_l, 2)} unit="L" />
          </div>
        </div>

        <div className="section">
          <h3>Where the points went</h3>
          {trip.breakdown.length === 0 ? (
            <p className="muted" style={{ margin: 0 }}>No risky events on this trip. Full marks.</p>
          ) : (
            <ul className="list breakdown">
              {trip.breakdown.map((b) => (
                <li key={b.type}>
                  <span><span className="code-chip">{b.type}</span>{b.name} x{b.count}</span>
                  <span className="pts">-{fmtNum(b.points, 1)}</span>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="section">
          {!coach ? (
            <button className="btn primary block" onClick={() => askCoach()} disabled={coachLoading}>
              <Sparkles size={16} />{coachLoading ? "Asking your coach" : "Explain my score"}
            </button>
          ) : (
            <div className="ai-panel" aria-live="polite">
              <div className="ai-head">
                <Sparkles size={16} color="var(--green)" />AI driving coach
                <ModelBadge result={coach} />
              </div>
              <Markdown text={coach.answer} />
              <Sources sources={coach.sources} />
              {coach.facts && (
                <details className="facts">
                  <summary>What the coach was told</summary>
                  <pre>{coach.facts}</pre>
                </details>
              )}
              <button className="link-btn" style={{ marginTop: 10 }} onClick={() => askCoach(true)} disabled={coachLoading}>
                <RefreshCw size={12} /> {coachLoading ? "Regenerating" : "Regenerate"}
              </button>
            </div>
          )}
        </div>

        {trip.events.length > 0 && (
          <div className="section">
            <h3>Events on this trip</h3>
            <ul className="list timeline">
              {trip.events.map((e) => {
                const off = Math.round((new Date(e.time) - new Date(trip.start_time)) / 1000);
                return (
                  <li key={e.id}>
                    <span className="t">{String(Math.floor(off / 60)).padStart(2, "0")}:{String(off % 60).padStart(2, "0")}</span>
                    <span><span className="code-chip">{e.type}</span>{e.name}<br /><span className="muted">{e.detail}</span></span>
                  </li>
                );
              })}
            </ul>
          </div>
        )}

        <a className="btn" href={api.tripExportUrl(tripId)} download><Download size={16} />Download trip data (CSV)</a>
      </div>
    </div>
  );
}

const Stat = ({ label, value, unit }) => (
  <div>
    <div className="metric-label">{label}</div>
    <div className="metric-value">{value}{unit && <small>{unit}</small>}</div>
  </div>
);
