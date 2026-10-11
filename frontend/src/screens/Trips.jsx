import { Search } from "lucide-react";
import { useEffect, useState } from "react";
import { Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../api";
import RouteThumb from "../components/RouteThumb";
import { fmtDate, fmtNum, fmtTime, Gauge, Pips, ScoreSign } from "../components/ui";
import { useLive } from "../live";
import TripDetail from "./TripDetail";

export default function Trips({ vehicle, openTrip, setOpenTrip, wide }) {
  const [tab, setTab] = useState("history");
  // Phone: the list and a trip's detail are separate screens. Desktop: side by side.
  if (openTrip && !wide) {
    return <div className="panel" style={{ padding: 0, overflow: "hidden" }}>
      <TripDetail tripId={openTrip} onBack={() => setOpenTrip(null)} />
    </div>;
  }
  return (
    <>
      <div className="segmented" role="group" aria-label="Trips view" style={{ maxWidth: 520 }}>
        {[["history", "Trip history"], ["analysis", "Driving analysis"], ["leaderboard", "Leaderboard"]].map(([k, l]) => (
          <button key={k} aria-pressed={tab === k} onClick={() => setTab(k)}>{l}</button>
        ))}
      </div>
      {tab === "history" && (
        <div className={wide ? "trips-split" : ""}>
          <div className="panel">
            <History vehicle={vehicle} onOpen={setOpenTrip} selected={wide ? openTrip : null}
                     onLoaded={(list) => wide && !openTrip && list.length && setOpenTrip(list[0].id)} />
          </div>
          {wide && (
            <div className="detail-col panel" style={{ padding: 0, overflow: "hidden" }}>
              {openTrip ? <TripDetail tripId={openTrip} /> : <div className="empty">Select a trip</div>}
            </div>
          )}
        </div>
      )}
      {tab === "analysis" && <div className="panel narrow"><Analysis vehicle={vehicle} /></div>}
      {tab === "leaderboard" && <div className="panel narrow"><Leaderboard vehicle={vehicle} /></div>}
    </>
  );
}

function History({ vehicle, onOpen, selected, onLoaded }) {
  const { version } = useLive();
  const [trips, setTrips] = useState(null);
  const [q, setQ] = useState("");
  useEffect(() => {
    const t = setTimeout(() => api.trips(vehicle.id, q).then((list) => {
      setTrips(list);
      onLoaded?.(list);
    }).catch(() => setTrips([])), 200);
    return () => clearTimeout(t);
  }, [vehicle.id, q, version]); // eslint-disable-line react-hooks/exhaustive-deps

  let lastDay = null;
  return (
    <>
      <label className="search">
        <Search size={16} className="muted" />
        <input placeholder="Search by place or trip name" value={q} onChange={(e) => setQ(e.target.value)} />
      </label>
      {trips === null && <div className="empty">Loading trips</div>}
      {trips?.length === 0 && (
        <div className="empty">No trips yet. Start a trip from the demo console, or run the feeder with --seed-days 7.</div>
      )}
      <ul className="list">
        {trips?.map((t) => {
          const day = fmtDate(t.start_time);
          const header = day !== lastDay ? day : null;
          lastDay = day;
          return (
            <li key={t.id}>
              {header && <div className="muted" style={{ fontSize: "var(--fs-xs)", fontWeight: 700, paddingTop: 12 }}>{header}</div>}
              <button className="trip-item" onClick={() => onOpen(t.id)} aria-current={selected === t.id ? "true" : undefined}>
                <RouteThumb points={t.route_preview} />
                <div style={{ minWidth: 0 }}>
                  <div className="places">{t.name || `${short(t.start_place)} to ${short(t.end_place)}`}</div>
                  <div className="meta">
                    <span>{fmtTime(t.start_time)}-{fmtTime(t.end_time)}</span>
                    <span>{fmtNum(t.distance_km, 2)} km</span>
                    {t.fuel_economy_kmpl && <span>{fmtNum(t.fuel_economy_kmpl, 1)} km/l</span>}
                  </div>
                </div>
                <ScoreSign score={t.score} size="sm" />
              </button>
            </li>
          );
        })}
      </ul>
    </>
  );
}

const short = (place) => (place || "").replace(", Dearborn", "");

function Analysis({ vehicle }) {
  const { version } = useLive();
  const [days, setDays] = useState(7);
  const [data, setData] = useState(null);
  useEffect(() => {
    api.analysis(vehicle.id, days).then(setData).catch(() => setData(null));
  }, [vehicle.id, days, version]);
  if (!data) return <div className="empty">Loading analysis</div>;
  const s = data.stats;
  return (
    <>
      <div className="segmented" role="group" aria-label="Period">
        {[7, 30].map((d) => <button key={d} aria-pressed={days === d} onClick={() => setDays(d)}>Last {d} days</button>)}
      </div>
      <div style={{ display: "grid", placeItems: "center" }}>
        <Gauge score={data.score} />
        <div className="muted" style={{ fontSize: "var(--fs-xs)", marginTop: 4 }}>
          Weekly score is the distance-weighted average of your trip scores
        </div>
      </div>
      <div className="section">
        <div className="stats-grid">
          <Stat label="Distance" value={fmtNum(s.distance_km, 1)} unit="km" />
          <Stat label="Trips" value={s.trips} />
          <Stat label="Max speed" value={fmtNum(s.max_speed_kph)} unit="km/h" />
          <Stat label="Avg speed" value={fmtNum(s.avg_speed_kph, 1)} unit="km/h" />
          <Stat label="Travel time" value={`${Math.floor(s.travel_time_s / 3600)}h ${String(Math.floor((s.travel_time_s % 3600) / 60)).padStart(2, "0")}m`} />
          <Stat label="Fuel economy" value={fmtNum(s.fuel_economy_kmpl, 1)} unit="km/l" />
        </div>
      </div>
      <div className="section">
        <h3>Distance per day</h3>
        <div style={{ height: 150 }}>
          <ResponsiveContainer>
            <BarChart data={data.daily} margin={{ top: 4, right: 0, left: -24, bottom: 0 }}>
              <XAxis dataKey="date" tickFormatter={(d) => new Date(d + "T12:00:00").toLocaleDateString([], { weekday: "narrow" })}
                     tick={{ fontSize: 11, fill: "#5c646c" }} axisLine={false} tickLine={false} interval={days > 7 ? 4 : 0} />
              <YAxis tick={{ fontSize: 11, fill: "#5c646c" }} axisLine={false} tickLine={false} width={46} />
              <Tooltip formatter={(v) => [`${v} km`, "Distance"]} labelFormatter={(d) => fmtDate(d + "T12:00:00")} />
              <Bar dataKey="distance_km" fill="#0E6B4E" radius={[3, 3, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>
      <div className="section">
        <h3>All events</h3>
        <ul className="list">
          {data.events.map((e) => (
            <li key={e.type} className="event-row">
              <span style={{ fontWeight: 600 }}>{e.name} <span className="muted">({e.type})</span></span>
              <span className="row" style={{ gap: 8 }}><span className="num muted">{e.count}</span><Pips level={e.level} /></span>
              <span className="msg">{e.message}</span>
            </li>
          ))}
        </ul>
      </div>
    </>
  );
}

const Stat = ({ label, value, unit }) => (
  <div>
    <div className="metric-label">{label}</div>
    <div className="metric-value">{value}{unit && <small>{unit}</small>}</div>
  </div>
);

function Leaderboard({ vehicle }) {
  const { version } = useLive();
  const [scope, setScope] = useState("all");
  const [lb, setLb] = useState(null);
  useEffect(() => {
    api.leaderboard(vehicle.id, scope).then(setLb).catch(() => setLb(null));
  }, [vehicle.id, scope, version]);
  return (
    <>
      <div className="segmented" role="group" aria-label="Leaderboard scope">
        <button aria-pressed={scope === "all"} onClick={() => setScope("all")}>All drivers</button>
        <button aria-pressed={scope === "model"} onClick={() => setScope("model")}>{vehicle.model} drivers</button>
      </div>
      {!lb && <div className="empty">Loading leaderboard</div>}
      {lb && lb.rank == null && <div className="empty">Complete a trip to get ranked.</div>}
      {lb && lb.rank != null && (
        <>
          <div className="rank-hero section">
            <div>
              <div className="metric-label">Your rank</div>
              <div className="rank">{fmtNum(lb.rank)}</div>
              <div className="muted" style={{ fontSize: "var(--fs-sm)" }}>
                of {fmtNum(lb.total)} drivers. You are ahead of {fmtNum(lb.ahead_of_pct, 1)}% of them.
              </div>
            </div>
            <ScoreSign score={Math.round(lb.score)} size="lg" />
          </div>
          <div className="section">
            <h3>Around you</h3>
            {lb.around.map((r) => <LbRow key={r.member} r={r} />)}
          </div>
          <div className="section">
            <h3>Top drivers</h3>
            {lb.top.slice(0, 5).map((r) => <LbRow key={r.member} r={r} />)}
          </div>
          <p className="hint">Ranks use each driver's 7-day score. Kept in a sorted set in the live layer, so finding
            your rank among {fmtNum(lb.total)} drivers takes about a millisecond.</p>
        </>
      )}
    </>
  );
}

const LbRow = ({ r }) => (
  <div className={`lb-row ${r.is_me ? "me" : ""}`}>
    <span className="r">#{fmtNum(r.rank)}</span>
    <span>{r.is_me ? `${r.label} (you)` : r.label}</span>
    <span className="num" style={{ fontSize: "var(--fs-md)" }}>{fmtNum(r.score, 1)}</span>
  </div>
);
