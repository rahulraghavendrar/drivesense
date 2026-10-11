// Thin wrapper around the FastAPI backend. In development, Vite proxies /api and /ws
// to http://localhost:8000 (see vite.config.js). Set VITE_API_URL for other setups.
const BASE = import.meta.env.VITE_API_URL || "";

async function request(method, path, body) {
  const res = await fetch(BASE + path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail || detail;
    } catch { /* not JSON */ }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.status === 204 ? null : res.json();
}

export const api = {
  vehicles: () => request("GET", "/api/vehicles"),
  status: () => request("GET", "/api/system/status"),
  trips: (vid, q) => request("GET", `/api/vehicles/${vid}/trips?limit=40${q ? `&q=${encodeURIComponent(q)}` : ""}`),
  trip: (tid) => request("GET", `/api/trips/${tid}`),
  renameTrip: (tid, name) => request("PATCH", `/api/trips/${tid}`, { name }),
  tripExportUrl: (tid) => `${BASE}/api/trips/${tid}/export.csv`,
  coach: (tid, refresh = false) => request("POST", `/api/trips/${tid}/coach${refresh ? "?refresh=true" : ""}`),
  analysis: (vid, days) => request("GET", `/api/vehicles/${vid}/analysis?days=${days}`),
  leaderboard: (vid, scope) => request("GET", `/api/vehicles/${vid}/leaderboard?scope=${scope}`),
  health: (vid) => request("GET", `/api/vehicles/${vid}/health`),
  batteryTrend: (vid) => request("GET", `/api/vehicles/${vid}/battery-trend`),
  diagnose: (aid, refresh = false) => request("POST", `/api/alerts/${aid}/diagnose${refresh ? "?refresh=true" : ""}`),
  chat: (vehicle_id, question, history) => request("POST", "/api/coach/chat", { vehicle_id, question, history }),
  command: (vid, action) => request("POST", `/api/vehicles/${vid}/commands`, { action }),
  geofences: (vid) => request("GET", `/api/vehicles/${vid}/geofences`),
  addGeofence: (vid, g) => request("POST", `/api/vehicles/${vid}/geofences`, g),
  deleteGeofence: (vid, gid) => request("DELETE", `/api/vehicles/${vid}/geofences/${gid}`),
  notifications: (vid) => request("GET", `/api/vehicles/${vid}/notifications`),
  markRead: (vid) => request("POST", `/api/vehicles/${vid}/notifications/read`),
  alertSettings: (vid) => request("GET", `/api/vehicles/${vid}/alert-settings`),
  saveAlertSettings: (vid, body) => request("PUT", `/api/vehicles/${vid}/alert-settings`, body),
  testAlert: (vid, severity) => request("POST", `/api/vehicles/${vid}/alerts/test?severity=${severity}`),
  report: (vid, refresh = false) => request("POST", `/api/vehicles/${vid}/report${refresh ? "?refresh=true" : ""}`),
  demoTrip: (vid, style, short) => request("POST", `/api/demo/${vid}/trip`, { style, short }),
  demoFault: (vid, fault) => request("POST", `/api/demo/${vid}/fault`, { fault }),
  demoClear: (vid) => request("POST", `/api/demo/${vid}/clear-faults`),
};

export function wsUrl() {
  if (BASE) return BASE.replace(/^http/, "ws") + "/ws";
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}/ws`;
}
