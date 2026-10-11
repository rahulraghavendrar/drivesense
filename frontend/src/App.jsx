import { Bell, FlaskConical, HeartPulse, House, Map as MapIcon, MessageSquareText, Route } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { primeAudio } from "./alerts";
import { api } from "./api";
import { AlertBanners, useAlertCenter } from "./components/AlertCenter";
import SimulatorDrawer from "./components/SimulatorDrawer";
import { EVENT_NAMES, fmtAgo, fmtNum } from "./components/ui";
import { useLive, useNow, useWide } from "./live";
import Alerts from "./screens/Alerts";
import Coach from "./screens/Coach";
import Health from "./screens/Health";
import Home from "./screens/Home";
import MapScreen from "./screens/MapScreen";
import Trips from "./screens/Trips";

const PAGES = [
  ["home", "My car", House],
  ["map", "Location", MapIcon],
  ["trips", "Trips", Route],
  ["health", "Health", HeartPulse],
  ["coach", "AI coach", MessageSquareText],
];
const TITLES = { home: "My car", map: "Location", trips: "Trips", health: "Health", coach: "AI coach", alerts: "Alerts" };

export default function App() {
  const { vehicles, states, subscribe, feed, connected } = useLive();
  const wide = useWide();
  const now = useNow(1000);
  const [tab, setTab] = useState("home");
  const [openTrip, setOpenTrip] = useState(null);
  const [unread, setUnread] = useState(0);
  const [toasts, setToasts] = useState([]);
  const [simOpen, setSimOpen] = useState(false);

  // This website monitors one personal car.
  const car = vehicles.find((v) => v.id === "my-car") || vehicles[0];
  const state = car ? states[car.id] : null;

  useEffect(() => primeAudio(), []);

  const navigate = useCallback(({ tab: t, trip }) => {
    setTab(t);
    setOpenTrip(trip ?? null);
    window.scrollTo({ top: 0 });
  }, []);

  const { banners, dismiss } = useAlertCenter(car?.id, navigate);
  const closeSim = useCallback(() => setSimOpen(false), []);

  useEffect(() => {
    if (car) api.notifications(car.id).then((d) => setUnread(d.unread)).catch(() => {});
  }, [car?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const latest = feed[0];
    if (latest?.type === "notification" && latest.vehicle_id === car?.id && tab !== "alerts") setUnread((u) => u + 1);
  }, [feed]); // eslint-disable-line react-hooks/exhaustive-deps

  // Small toasts: live driving events and informational notifications (alerts get banners instead).
  useEffect(() => subscribe((item) => {
    if (!car || item.vehicle_id !== car.id) return;
    let text = null;
    let code = null;
    if (item.type === "driving_event") {
      code = item.data.type;
      text = `${EVENT_NAMES[code] || item.data.name}: ${item.data.detail}`;
    } else if (item.type === "notification" && item.data.severity === "info" && item.data.kind !== "test") {
      text = item.data.title;
    }
    if (!text) return;
    setToasts((t) => [...t.slice(-2), { key: item.key, text, code }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.key !== item.key)), 4500);
  }), [subscribe, car]);

  let body;
  if (!car) {
    body = (
      <div className="panel empty">
        No car connected yet. Start the backend, then the sensor simulator:<br />
        <code>python fake_sensors.py --reset --seed-days 14</code>
      </div>
    );
  } else if (tab === "home") body = <Home car={car} state={state} go={navigate} />;
  else if (tab === "map") body = <MapScreen vehicle={car} state={state} />;
  else if (tab === "trips") body = <Trips vehicle={car} openTrip={openTrip} setOpenTrip={setOpenTrip} wide={wide} />;
  else if (tab === "health") body = <Health vehicle={car} state={state} />;
  else if (tab === "coach") body = <Coach vehicle={car} />;
  else body = (
    <Alerts car={car} wide={wide} onRead={() => setUnread(0)}
            onOpen={(n) => navigate(n.kind === "trip" && n.meta?.trip_id ? { tab: "trips", trip: n.meta.trip_id }
              : n.kind === "health" ? { tab: "health" } : n.kind === "geofence" ? { tab: "map" } : { tab: "alerts" })} />
  );

  const carState = state ? (state.ignition ? `Driving at ${fmtNum(state.speed_kph)} km/h` : "Parked") : "Waiting for data";

  return (
    <div className="shell">
      <aside className="sidebar" aria-label="Main navigation">
        <div className="brand-plate">DriveSense</div>
        {car && (
          <div className="car-block">
            <div className="car-name">{car.name}</div>
            <div className="car-sub">{car.model}, {car.plate}</div>
            <div className="car-state"><span className={`dot ${state?.ignition ? "drive" : "ok"}`} />{carState}</div>
          </div>
        )}
        <nav className="side-nav">
          {PAGES.map(([key, label, Icon]) => (
            <button key={key} aria-current={tab === key ? "page" : undefined} onClick={() => navigate({ tab: key })}>
              <Icon size={18} />{label}
            </button>
          ))}
          <button aria-current={tab === "alerts" ? "page" : undefined} onClick={() => navigate({ tab: "alerts" })}>
            <Bell size={18} />Alerts{unread > 0 && <span className="count">{unread > 99 ? "99+" : unread}</span>}
          </button>
        </nav>
        <div className="side-foot">
          <span className="row" style={{ gap: 7 }}>
            <span className={`dot ${connected ? "ok" : "warn"}`} />{connected ? "Live connection" : "Reconnecting"}
          </span>
          {car && <button className="sim-btn" onClick={() => setSimOpen(true)}><FlaskConical size={16} />Simulator</button>}
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <h1>{tab === "home" && car ? car.name : TITLES[tab]}</h1>
          <span className="updated">{state ? `${carState}, updated ${fmtAgo(state.ts, now)}` : ""}</span>
          {car && (
            <button className="icon-btn mobile-only" aria-label="Simulator" onClick={() => setSimOpen(true)}
                    style={{ marginLeft: "auto" }}>
              <FlaskConical size={17} />
            </button>
          )}
          <button className="icon-btn" aria-label={`Alerts, ${unread} unread`} onClick={() => navigate({ tab: "alerts" })}>
            <Bell size={18} />
            {unread > 0 && <span className="badge">{unread > 99 ? "99+" : unread}</span>}
          </button>
        </header>

        <main className="content">
          <AlertBanners banners={banners} dismiss={dismiss} navigate={navigate} />
          {body}
        </main>
      </div>

      <nav className="tabbar" aria-label="Main">
        {PAGES.map(([key, label, Icon]) => (
          <button key={key} aria-current={tab === key ? "page" : undefined} onClick={() => navigate({ tab: key })}>
            <Icon size={20} />{label === "AI coach" ? "Coach" : label === "My car" ? "Home" : label}
          </button>
        ))}
      </nav>

      <div className="toast-stack" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.key} className="toast">
            {t.code && <span className="code-chip">{t.code}</span>}
            <span>{t.text}</span>
          </div>
        ))}
      </div>

      {simOpen && car && <SimulatorDrawer car={car} onClose={closeSim} />}
    </div>
  );
}
