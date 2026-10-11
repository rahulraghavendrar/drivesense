import { AlertTriangle, BellRing, Fuel, Gauge, MapPinned, Moon, Route, Wrench, Zap } from "lucide-react";
import { useEffect, useState } from "react";
import { desktopPermission, getPrefs, onPrefsChange, requestDesktopPermission, setPrefs } from "../alerts";
import { api } from "../api";
import { fmtAgo, Toggle } from "../components/ui";
import { useLive, useNow } from "../live";

const ICONS = { trip: Route, geofence: MapPinned, health: Wrench, fuel: Fuel, speed: Gauge, night: Moon,
  driving: Zap, test: BellRing };

export default function Alerts({ car, wide, onOpen, onRead }) {
  const [tab, setTab] = useState("inbox");
  if (wide) {
    return (
      <div className="grid-2">
        <div className="panel"><h2>Notifications</h2><Inbox car={car} onOpen={onOpen} onRead={onRead} /></div>
        <AlertSettings car={car} />
      </div>
    );
  }
  return (
    <>
      <div className="segmented" role="group" aria-label="Alerts view">
        <button aria-pressed={tab === "inbox"} onClick={() => setTab("inbox")}>Notifications</button>
        <button aria-pressed={tab === "settings"} onClick={() => setTab("settings")}>Alert settings</button>
      </div>
      {tab === "inbox" ? <div className="panel"><Inbox car={car} onOpen={onOpen} onRead={onRead} /></div>
        : <AlertSettings car={car} />}
    </>
  );
}

function Inbox({ car, onOpen, onRead }) {
  const { feed } = useLive();
  const now = useNow(10000);
  const [data, setData] = useState(null);
  useEffect(() => {
    api.notifications(car.id).then((d) => {
      setData(d);
      if (d.unread) api.markRead(car.id).then(onRead).catch(() => {});
    }).catch(() => setData({ unread: 0, items: [] }));
  }, [car.id, feed.length]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!data) return <div className="empty">Loading</div>;
  if (!data.items.length) {
    return <div className="empty">No notifications yet. Trips, alerts and geofence crossings appear here.</div>;
  }
  return (
    <ul className="list">
      {data.items.map((n) => {
        const Icon = ICONS[n.kind] || AlertTriangle;
        return (
          <li key={n.id}>
            <button className={`notif ${n.is_read ? "" : "unread"}`} onClick={() => onOpen(n)}
                    style={{ border: 0, background: "none", width: "100%", textAlign: "left", color: "inherit" }}>
              <span className={`ico ${n.severity}`}><Icon size={15} /></span>
              <span>
                <span className="title">{n.title}</span>
                <span className="body" style={{ display: "block" }}>{n.body}</span>
                <span className="faint" style={{ fontSize: 11 }}>{fmtAgo(n.time, now)}</span>
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

function AlertSettings({ car }) {
  const [form, setForm] = useState(null);
  const [status, setStatus] = useState("");
  const [prefs, setLocalPrefs] = useState(getPrefs());
  const [perm, setPerm] = useState(desktopPermission());
  const browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";

  useEffect(() => onPrefsChange(setLocalPrefs), []);
  useEffect(() => {
    api.alertSettings(car.id).then((s) => setForm({
      speedOn: s.speed_alert_kph != null,
      speed: s.speed_alert_kph ?? 100,
      fuel: s.low_fuel_pct,
      nightOn: !!s.night_start,
      nightStart: s.night_start || "23:00",
      nightEnd: s.night_end || "05:00",
      harsh: s.harsh_driving,
      geofence: s.geofence,
    })).catch(() => setStatus("Could not load settings."));
  }, [car.id]);

  if (!form) return <div className="panel"><div className="empty">{status || "Loading settings"}</div></div>;
  const set = (patch) => { setForm({ ...form, ...patch }); setStatus(""); };

  const save = async () => {
    setStatus("Saving");
    try {
      await api.saveAlertSettings(car.id, {
        speed_alert_kph: form.speedOn ? Number(form.speed) : null,
        low_fuel_pct: Number(form.fuel),
        night_start: form.nightOn ? form.nightStart : null,
        night_end: form.nightOn ? form.nightEnd : null,
        timezone: browserTz,
        harsh_driving: form.harsh,
        geofence: form.geofence,
      });
      setStatus("Saved. The car's data is checked against these rules from the next reading.");
    } catch (e) {
      setStatus(`Not saved: ${e.message}`);
    }
  };

  const test = async (severity) => {
    try {
      await api.testAlert(car.id, severity);
      setStatus(`Test ${severity} alert sent. It travels the same path as a real alert.`);
    } catch (e) {
      setStatus(`Test failed: ${e.message}`);
    }
  };

  return (
    <div>
      <div className="panel">
        <h2>What should alert you</h2>
        <div className="setting">
          <span className="name">Engine, battery and tyre problems</span>
          <Toggle checked disabled label="Health alerts" />
          <span className="desc">Always on for safety, including predicted problems with a countdown.</span>
        </div>
        <div className="setting">
          <span className="name">Speed alert</span>
          <Toggle checked={form.speedOn} onChange={(v) => set({ speedOn: v })} label="Speed alert" />
          <span className="desc">Alerts you when the car goes faster than this for more than 3 seconds.</span>
          {form.speedOn && (
            <span className="extra">
              Above <input type="number" min="20" max="250" value={form.speed} aria-label="Speed limit for the alert"
                           onChange={(e) => set({ speed: e.target.value })} /> km/h
            </span>
          )}
        </div>
        <div className="setting">
          <span className="name">Low fuel</span>
          <span className="num" style={{ fontSize: "var(--fs-lg)" }}>{form.fuel}%</span>
          <span className="extra">
            <input type="range" min="5" max="50" step="1" value={form.fuel} className="grow" aria-label="Low fuel level"
                   onChange={(e) => set({ fuel: Number(e.target.value) })} />
          </span>
        </div>
        <div className="setting">
          <span className="name">Night-time use</span>
          <Toggle checked={form.nightOn} onChange={(v) => set({ nightOn: v })} label="Night-time use alert" />
          <span className="desc">Alerts you if the car is started during these hours ({browserTz}).</span>
          {form.nightOn && (
            <span className="extra">
              From <input type="time" value={form.nightStart} onChange={(e) => set({ nightStart: e.target.value })} aria-label="Start" />
              to <input type="time" value={form.nightEnd} onChange={(e) => set({ nightEnd: e.target.value })} aria-label="End" />
            </span>
          )}
        </div>
        <div className="setting">
          <span className="name">Harsh driving</span>
          <Toggle checked={form.harsh} onChange={(v) => set({ harsh: v })} label="Harsh driving alerts" />
          <span className="desc">An alert for every harsh brake, acceleration or turn. Useful when someone else drives your car.</span>
        </div>
        <div className="setting">
          <span className="name">Geofences</span>
          <Toggle checked={form.geofence} onChange={(v) => set({ geofence: v })} label="Geofence alerts" />
          <span className="desc">Alerts you when the car enters or leaves an area you drew on the map.</span>
        </div>
        <div className="save-row">
          <button className="btn primary" onClick={save}>Save alert settings</button>
          <span className="muted" role="status" style={{ fontSize: "var(--fs-sm)" }}>{status}</span>
        </div>
      </div>

      <div className="panel">
        <h2>How this device alerts you</h2>
        <div className="setting">
          <span className="name">Desktop notifications</span>
          {perm === "granted" ? (
            <Toggle checked={prefs.desktop} onChange={(v) => setPrefs({ desktop: v })} label="Desktop notifications" />
          ) : (
            <button className="btn" disabled={perm === "denied" || perm === "unsupported"}
                    onClick={async () => setPerm(await requestDesktopPermission())}>Allow</button>
          )}
          <span className="desc">
            {perm === "denied" ? "Blocked in this browser. Allow notifications for this site in the browser's site settings."
              : perm === "unsupported" ? "This browser does not support notifications."
              : "Shows a system notification when the site is in the background, so you never miss an alert."}
          </span>
        </div>
        <div className="setting">
          <span className="name">Alert sound</span>
          <Toggle checked={prefs.sound} onChange={(v) => setPrefs({ sound: v })} label="Alert sound" />
          <span className="desc">A short tone for warnings and a repeating tone for critical alerts.</span>
        </div>
        <div className="save-row">
          <button className="btn" onClick={() => test("warning")}><BellRing size={15} />Send a test warning</button>
          <button className="btn" onClick={() => test("critical")}>Send a test critical alert</button>
        </div>
      </div>
    </div>
  );
}
