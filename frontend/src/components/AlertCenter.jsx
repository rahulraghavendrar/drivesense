// Turns live notifications into alerts the owner cannot miss:
//   warning/watch -> yellow banner (+ short tone)          critical -> red banner (+ repeated tone)
//   tab in the background -> operating-system notification
//   any unacknowledged alert -> the tab title shows "(2) Coolant critical - DriveSense"
import { AlertOctagon, AlertTriangle } from "lucide-react";
import { useEffect, useState } from "react";
import { playAlertSound, showDesktopNotification } from "../alerts";
import { api } from "../api";
import { useLive } from "../live";

const ALERT_SEVERITIES = new Set(["watch", "warning", "critical"]);

/** Which screen explains a given alert. */
export function screenFor(n) {
  if (n.kind === "health") return { tab: "health" };
  if (n.kind === "geofence") return { tab: "map" };
  if (n.kind === "trip" && n.meta?.trip_id) return { tab: "trips", trip: n.meta.trip_id };
  if (n.kind === "speed" || n.kind === "driving" || n.kind === "night" || n.kind === "fuel") return { tab: "home" };
  return { tab: "alerts" };
}

// Health notifications carry the id of the health alert they belong to (opened, escalated, resolved).
const alertId = (n) => (n.kind === "health" ? n.meta?.alert_id ?? null : null);
const sameAlert = (a, b) => alertId(a) !== null && alertId(a) === alertId(b);

export function useAlertCenter(carId, navigate) {
  const { subscribe } = useLive();
  const [banners, setBanners] = useState([]);

  useEffect(() => subscribe((item) => {
    if (item.type !== "notification" || item.vehicle_id !== carId) return;
    const n = item.data;
    const isAlert = ALERT_SEVERITIES.has(n.severity) || n.kind === "test";
    if (!isAlert) {
      // "Resolved: ..." - the problem is gone, so its banner goes too.
      if (alertId(n) !== null) setBanners((b) => b.filter((x) => !sameAlert(x, n)));
      return;
    }
    // An escalation (warning -> critical) replaces the banner of the same alert.
    setBanners((b) => [n, ...b.filter((x) => x.id !== n.id && !sameAlert(x, n))].slice(0, 4));
    playAlertSound(n.severity);
    if (n.kind === "test" || document.visibilityState !== "visible" || !document.hasFocus()) {
      showDesktopNotification(n, () => navigate(screenFor(n)));
    }
  }), [subscribe, carId, navigate]);

  // Opening the site after an alert fired: show recent unread alerts straight away.
  useEffect(() => {
    if (!carId) return;
    api.notifications(carId).then((d) => {
      // Items are newest first: keep only the latest notification per health alert, and skip
      // alerts that have been resolved since.
      const seen = new Set();
      const recent = d.items.filter((n) => {
        const id = alertId(n);
        if (id !== null) {
          if (seen.has(id)) return false;
          seen.add(id);
        }
        return !n.is_read && ALERT_SEVERITIES.has(n.severity) && Date.now() - new Date(n.time).getTime() < 15 * 60000;
      });
      const critical = recent.filter((n) => n.severity === "critical");
      const initial = (critical.length ? critical : recent).slice(0, 3);
      setBanners((b) => [...b, ...initial.filter((n) => !b.some((x) => x.id === n.id))].slice(0, 4));
    }).catch(() => {});
  }, [carId]);

  // Warnings tidy themselves away after a minute; critical alerts stay until dismissed.
  useEffect(() => {
    const t = setInterval(() => {
      setBanners((b) => b.filter((n) => n.severity === "critical" || Date.now() - new Date(n.time).getTime() < 60000));
    }, 5000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    document.title = banners.length ? `(${banners.length}) ${banners[0].title} - DriveSense` : "DriveSense";
  }, [banners]);

  const dismiss = (id) => setBanners((b) => b.filter((n) => n.id !== id));
  return { banners, dismiss };
}

export function AlertBanners({ banners, dismiss, navigate }) {
  if (!banners.length) return null;
  return (
    <div className="alert-stack" role="alert" aria-live="assertive">
      {banners.map((n) => {
        const critical = n.severity === "critical";
        const Icon = critical ? AlertOctagon : AlertTriangle;
        return (
          <div key={n.id} className={`alert-banner ${critical ? "critical" : ""}`}>
            <Icon size={22} />
            <div>
              <div className="t">{n.title}</div>
              {n.body && <div className="b">{n.body}</div>}
            </div>
            <div className="acts">
              <button className="primary" onClick={() => { navigate(screenFor(n)); dismiss(n.id); }}>View</button>
              <button onClick={() => dismiss(n.id)}>Dismiss</button>
            </div>
          </div>
        );
      })}
    </div>
  );
}
