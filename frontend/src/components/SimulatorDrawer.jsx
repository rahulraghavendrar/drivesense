import { X } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../api";
import { useLive } from "../live";
import { fmtNum } from "./ui";

const FAULTS = [
  ["coolant_leak", "Coolant leak", "engine temperature climbs while driving"],
  ["alternator_fault", "Alternator fault", "charging voltage drops while driving"],
  ["slow_puncture", "Slow puncture", "one tyre loses pressure, parked or driving"],
  ["weak_battery", "Weak battery", "resting voltage drops while parked"],
];

function describe(item) {
  const d = item.data || {};
  if (item.type === "driving_event") return `${d.name}: ${d.detail}`;
  if (item.type === "notification") return d.title;
  if (item.type === "trip_completed") return `Trip completed, score ${d.score}`;
  return item.type;
}

/** Stands in for real life during a demo: makes the simulated car drive, or develop a fault.
 *  It only queues commands; the fake sensor feeder picks them up within a second. */
export default function SimulatorDrawer({ car, onClose }) {
  const { connected, feed } = useLive();
  const [status, setStatus] = useState(null);
  const [longTrip, setLongTrip] = useState(true);
  const [flash, setFlash] = useState("");

  useEffect(() => {
    const load = () => api.status().then(setStatus).catch(() => setStatus(null));
    load();
    const t = setInterval(load, 5000);
    const esc = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", esc);
    return () => { clearInterval(t); window.removeEventListener("keydown", esc); };
  }, [onClose]);

  const act = async (label, fn) => {
    try {
      await fn();
      setFlash(`${label}: sent. The car reacts within a second.`);
    } catch (e) {
      setFlash(`${label} failed: ${e.message}`);
    }
    setTimeout(() => setFlash(""), 6000);
  };

  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-modal="true" aria-label="Simulator">
        <button className="cbtn close" onClick={onClose} aria-label="Close simulator"><X size={16} /></button>
        <h2>Simulator</h2>
        <p className="lede">
          There is no real car connected, so a sensor simulator plays the car. Use these controls to make it drive
          or break down, and watch the website react and raise alerts.
        </p>

        <div className="sys">
          <div><span className={`dot ${status?.simulator_online ? "ok" : "warn"}`} />
            Sensor simulator {status?.simulator_online ? "streaming" : "not running (start fake_sensors.py)"}</div>
          <div><span className={`dot ${connected ? "ok" : "warn"}`} />Live connection {connected ? "open" : "reconnecting"}</div>
          <div><span className={`dot ${status?.llm?.enabled ? "ok" : "warn"}`} />
            {status?.llm?.enabled ? `AI: ${status.llm.model}` : "AI: offline answers (no Gemini key)"}</div>
          <div><span className={`dot ${status?.database ? "ok" : "warn"}`} />
            {status?.database
              ? `${status.database.provider} ${status.database.version}, ${status.database.timescaledb
                  ? "TimescaleDB" : "BRIN time index"}, ${fmtNum(status.database.round_trip_ms, 1)} ms round trip`
              : "Database: checking"}</div>
          <div><span className={`dot ${status?.hot_layer ? "ok" : "warn"}`} />
            Live layer: {status?.hot_layer === "memory" ? "in-process (no Redis)" : status?.hot_layer === "redis" ? "Redis" : "checking"},
            ingest {status?.ingest_latency_ms?.p50 != null
              ? `${fmtNum(status.ingest_latency_ms.p50, 1)} ms median` : "latency pending"}</div>
        </div>

        <h3>Drive the car</h3>
        <div className="btns">
          {["calm", "normal", "aggressive"].map((style) => (
            <button key={style} className={`cbtn ${style === "aggressive" ? "warn" : "go"}`}
                    onClick={() => act(`${style} trip`, () => api.demoTrip(car.id, style, !longTrip))}>
              {style[0].toUpperCase() + style.slice(1)} trip
            </button>
          ))}
        </div>
        <label className="check">
          <input type="checkbox" checked={longTrip} onChange={(e) => setLongTrip(e.target.checked)} />Longer route
        </label>

        <h3>Simulate a problem</h3>
        <div className="btns">
          {FAULTS.map(([key, label]) => (
            <button key={key} className="cbtn warn" onClick={() => act(label, () => api.demoFault(car.id, key))}>{label}</button>
          ))}
          <button className="cbtn" onClick={() => act("Repair", () => api.demoClear(car.id))}>Repair all</button>
        </div>
        <ul className="fault-help">
          {FAULTS.map(([key, label, help]) => <li key={key}>{label}: {help}</li>)}
        </ul>
        <p className="lede" style={{ marginTop: 10 }}>
          Tip: inject a coolant leak, then start a longer trip. Within about 3 minutes of driving the website predicts
          the overheating and alerts you before the limit is reached.
        </p>
        {flash && <p role="status" style={{ color: "#9fe0c4" }}>{flash}</p>}

        <h3>Live events</h3>
        <div className="log">
          {feed.length === 0 && <div><span /><span>Waiting for events</span></div>}
          {feed.slice(0, 40).map((f) => (
            <div key={f.key}>
              <span>{new Date(f.at).toLocaleTimeString([], { hour12: false })}</span>
              <span>{describe(f)}</span>
            </div>
          ))}
        </div>
      </aside>
    </>
  );
}
