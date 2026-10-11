import { Sparkles, Wrench } from "lucide-react";
import { useEffect, useState } from "react";
import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../api";
import { fmtAgo, fmtNum, Markdown, ModelBadge, RangeBar, Sources } from "../components/ui";
import { useNow } from "../live";

// Mirrors the backend rules (app/engine/health.py) so the bars can show the bands.
const RULES = {
  coolant: { band: [70, 102], warn: 105, crit: 110, direction: "up" },
  charging: { band: [13.4, 14.9], warn: 13.2, crit: 12.6, direction: "down" },
  battery_rest: { band: [12.3, 13.0], warn: 12.1, crit: 11.8, direction: "down" },
  tire_fl: { band: [32, 38], warn: 30, crit: 27, direction: "down" },
  tire_fr: { band: [32, 38], warn: 30, crit: 27, direction: "down" },
  tire_rl: { band: [32, 38], warn: 30, crit: 27, direction: "down" },
  tire_rr: { band: [32, 38], warn: 30, crit: 27, direction: "down" },
};
const STATUS_TEXT = { ok: "Normal", watch: "Trending", warning: "Warning", critical: "Critical" };

function eta(seconds) {
  if (seconds == null) return null;
  if (seconds >= 86400) return `${Math.round(seconds / 86400)} days`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return m ? `${m} min ${String(s).padStart(2, "0")} s` : `${s} s`;
}

export default function Health({ vehicle, state }) {
  const now = useNow(5000);
  const [trend, setTrend] = useState(null);
  const [history, setHistory] = useState([]);
  const [diag, setDiag] = useState({});
  const [loadingId, setLoadingId] = useState(null);

  useEffect(() => {
    api.batteryTrend(vehicle.id).then(setTrend).catch(() => setTrend(null));
    api.health(vehicle.id).then((h) => setHistory(h.history)).catch(() => setHistory([]));
    setDiag({});
  }, [vehicle.id, Math.floor(now / 60000)]); // refresh once a minute

  const health = state?.health || { signals: {}, alerts: [] };
  const alerts = [...health.alerts].sort((a, b) => (b.severity === "critical") - (a.severity === "critical"));

  const diagnose = async (alert, refresh = false) => {
    setLoadingId(alert.id);
    try {
      const r = await api.diagnose(alert.id, refresh);
      setDiag((d) => ({ ...d, [alert.id]: r }));
    } catch (e) {
      setDiag((d) => ({ ...d, [alert.id]: { answer: `Diagnosis unavailable: ${e.message}`, sources: [] } }));
    } finally {
      setLoadingId(null);
    }
  };

  return (
    <div className="grid-2">
      <div>
      <div className="panel">
        <h2>{alerts.length ? `Needs attention (${alerts.length})` : "No active alerts"}</h2>
        {!alerts.length && <p className="muted" style={{ margin: 0 }}>Every monitored system is inside its healthy range.</p>}
        {alerts.map((a) => (
          <div key={a.code} className={`alert-card ${a.severity}`}>
            <div className="row between">
              <strong>{a.title}</strong>
              <span className={`chip ${a.severity}`}>{a.kind === "predictive" ? "Predicted" : a.kind === "trend" ? "Trend" : STATUS_TEXT[a.severity]}</span>
            </div>
            {a.eta_seconds != null && (
              <div style={{ margin: "8px 0 4px" }}>
                <div className="metric-label">Critical limit in about</div>
                <div className="eta">{eta(a.eta_seconds)}</div>
              </div>
            )}
            <p style={{ margin: "6px 0", fontSize: "var(--fs-sm)" }}>{a.message}</p>
            {a.id && !diag[a.id] && (
              <button className="btn" onClick={() => diagnose(a)} disabled={loadingId === a.id}>
                <Wrench size={15} />{loadingId === a.id ? "Diagnosing" : "Diagnose with AI"}
              </button>
            )}
            {diag[a.id] && (
              <div className="ai-panel" style={{ marginTop: 8 }} aria-live="polite">
                <div className="ai-head"><Sparkles size={16} color="var(--green)" />AI diagnosis<ModelBadge result={diag[a.id]} /></div>
                <Markdown text={diag[a.id].answer} />
                <Sources sources={diag[a.id].sources} />
                {diag[a.id].facts && (
                  <details className="facts"><summary>What the AI was told</summary><pre>{diag[a.id].facts}</pre></details>
                )}
              </div>
            )}
          </div>
        ))}
      </div>
      <BatteryTrend trend={trend} />
      </div>

      <div>
      <div className="panel">
        <h2>Live sensors</h2>
        {Object.entries(health.signals).length === 0 && <p className="muted">Waiting for sensor data.</p>}
        {Object.entries(health.signals).map(([code, s]) => (
          <div key={code} className="signal">
            <div className="row between">
              <span>{s.label}</span>
              <span className="row" style={{ gap: 8 }}>
                <span className="value">{fmtNum(s.value, s.unit === "V" ? 2 : 1)}</span>
                <span className="muted" style={{ fontSize: "var(--fs-xs)" }}>{s.unit}</span>
                <span className={`chip ${s.status}`}>{STATUS_TEXT[s.status] || s.status}</span>
              </span>
            </div>
            {RULES[code] && <RangeBar value={s.value} {...RULES[code]} />}
          </div>
        ))}
        <p className="hint">Green is the healthy range, yellow the warning zone, red the critical zone. Values outside
          the green range that are moving toward red trigger a predicted alert with a countdown.</p>
      </div>

      <div className="panel">
        <h2>Alert history</h2>
        {history.length === 0 && <p className="muted">No alerts recorded yet.</p>}
        <ul className="list">
          {history.slice(0, 8).map((h) => (
            <li key={h.id} className="row between" style={{ padding: "8px 0", fontSize: "var(--fs-sm)" }}>
              <span className="grow">{h.title}</span>
              <span className="muted">{h.status === "active" ? "Active" : `Resolved ${fmtAgo(h.resolved_at, now)}`}</span>
            </li>
          ))}
        </ul>
      </div>
      </div>
    </div>
  );
}

function BatteryTrend({ trend }) {
  return (
    <div className="panel">
      <h2>12 V battery, resting voltage per day</h2>
        {trend?.days?.length ? (
          <>
            <div style={{ height: 160 }}>
              <ResponsiveContainer>
                <LineChart data={trend.days} margin={{ top: 8, right: 8, left: -16, bottom: 0 }}>
                  <CartesianGrid stroke="#eceff1" vertical={false} />
                  <XAxis dataKey="day" tickFormatter={(d) => d.slice(5)} tick={{ fontSize: 11, fill: "#5c646c" }} />
                  <YAxis domain={[11.8, 12.8]} tick={{ fontSize: 11, fill: "#5c646c" }} />
                  <Tooltip formatter={(v) => [`${v} V`, "Average resting voltage"]} />
                  <ReferenceLine y={12.0} stroke="#C62828" strokeDasharray="4 4"
                                 label={{ value: "May not start", fontSize: 10, fill: "#C62828", position: "insideBottomRight" }} />
                  <Line type="monotone" dataKey="avg_v" stroke="#0E6B4E" strokeWidth={2.5} dot={{ r: 3 }} />
                </LineChart>
              </ResponsiveContainer>
            </div>
            <p style={{ fontSize: "var(--fs-sm)", margin: "6px 0 0" }}>
              {trend.prediction ? trend.prediction.message : "The resting voltage is stable. No battery problem predicted."}
            </p>
          </>
        ) : (
          <p className="muted">Needs at least 3 days of parked readings. Seed history with --seed-days 14.</p>
        )}
      </div>
  );
}
