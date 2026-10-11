// Live data layer: one WebSocket for the whole app.
// Telemetry updates are buffered and flushed to React 4x per second, so 6+ messages per
// second never cause 6+ full re-renders per second.
import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { api, wsUrl } from "./api";

const LiveContext = createContext(null);

export function LiveProvider({ children }) {
  const [vehicles, setVehicles] = useState([]);
  const [states, setStates] = useState({});
  const [connected, setConnected] = useState(false);
  const [feed, setFeed] = useState([]);            // driving events, notifications, trip completions
  const [version, setVersion] = useState(0);       // bumps when a trip completes (lists refetch)
  const pending = useRef({});
  const listeners = useRef(new Set());

  const loadVehicles = useCallback(async () => {
    try {
      const list = await api.vehicles();
      setVehicles(list);
      setStates((prev) => {
        const next = { ...prev };
        for (const v of list) if (v.latest && !next[v.id]) next[v.id] = v.latest;
        return next;
      });
    } catch { /* backend not up yet - retried below */ }
  }, []);

  useEffect(() => {
    loadVehicles();
    const t = setInterval(loadVehicles, 15000);
    return () => clearInterval(t);
  }, [loadVehicles]);

  useEffect(() => {
    let ws;
    let closed = false;
    let retry;
    const flush = setInterval(() => {
      const batch = pending.current;
      if (Object.keys(batch).length) {
        pending.current = {};
        setStates((prev) => ({ ...prev, ...batch }));
      }
    }, 250);
    const ping = setInterval(() => ws && ws.readyState === 1 && ws.send("ping"), 20000);

    const connect = () => {
      ws = new WebSocket(wsUrl());
      ws.onopen = () => setConnected(true);
      ws.onclose = () => {
        setConnected(false);
        if (!closed) retry = setTimeout(connect, 2000);
      };
      ws.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        if (msg.type === "telemetry") {
          pending.current[msg.vehicle_id] = msg.data;
          return;
        }
        const item = { ...msg, at: Date.now(), key: `${Date.now()}-${Math.random()}` };
        setFeed((f) => [item, ...f].slice(0, 60));
        if (msg.type === "trip_completed") setVersion((v) => v + 1);
        listeners.current.forEach((fn) => fn(item));
      };
    };
    connect();
    return () => {
      closed = true;
      clearTimeout(retry);
      clearInterval(flush);
      clearInterval(ping);
      ws && ws.close();
    };
  }, []);

  const subscribe = useCallback((fn) => {
    listeners.current.add(fn);
    return () => listeners.current.delete(fn);
  }, []);

  return (
    <LiveContext.Provider value={{ vehicles, states, connected, feed, version, subscribe, reloadVehicles: loadVehicles }}>
      {children}
    </LiveContext.Provider>
  );
}

export const useLive = () => useContext(LiveContext);

/** Re-render every second (for "updated 3 s ago" style labels). */
export function useNow(intervalMs = 1000) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(t);
  }, [intervalMs]);
  return now;
}

/** True on desktop-sized screens (sidebar layout). */
export function useWide(query = "(min-width: 1024px)") {
  const [wide, setWide] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const mq = window.matchMedia(query);
    const fn = (e) => setWide(e.matches);
    mq.addEventListener("change", fn);
    return () => mq.removeEventListener("change", fn);
  }, [query]);
  return wide;
}
