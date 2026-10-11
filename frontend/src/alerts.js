// How THIS browser alerts the owner (saved in localStorage, per device):
//   sound    - a short tone for warnings, a repeated two-tone for critical alerts
//   desktop  - operating-system notifications (Notification API), so alerts reach you
//              even when the tab is in the background or minimised
// What triggers an alert is decided by the backend (health rules + the owner's alert settings).

const KEY = "ds.alertPrefs";
const DEFAULTS = { sound: true, desktop: true };
const listeners = new Set();

export function getPrefs() {
  try {
    return { ...DEFAULTS, ...JSON.parse(localStorage.getItem(KEY) || "{}") };
  } catch {
    return { ...DEFAULTS };
  }
}

export function setPrefs(patch) {
  const next = { ...getPrefs(), ...patch };
  try { localStorage.setItem(KEY, JSON.stringify(next)); } catch { /* storage blocked */ }
  listeners.forEach((fn) => fn(next));
  return next;
}

export function onPrefsChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

// ------------------------------------------------------------------ desktop notifications
export const desktopSupported = () => typeof window !== "undefined" && "Notification" in window;
export const desktopPermission = () => (desktopSupported() ? Notification.permission : "unsupported");

export async function requestDesktopPermission() {
  if (!desktopSupported()) return "unsupported";
  const result = await Notification.requestPermission();
  if (result === "granted") setPrefs({ desktop: true });
  return result;
}

export function showDesktopNotification(n, onClick) {
  if (!getPrefs().desktop || desktopPermission() !== "granted") return;
  try {
    const note = new Notification(n.title, {
      body: n.body || "",
      tag: `drivesense-${n.id}`,
      icon: "/icon.svg",
      requireInteraction: n.severity === "critical",
    });
    note.onclick = () => {
      window.focus();
      onClick?.();
      note.close();
    };
  } catch { /* some mobile browsers only allow notifications from a service worker */ }
}

// ---------------------------------------------------------------------------- sound
let ctx = null;

/** Browsers only allow audio after a user gesture, so unlock it on the first click/tap. */
export function primeAudio() {
  const unlock = () => {
    try {
      ctx = ctx || new (window.AudioContext || window.webkitAudioContext)();
      if (ctx.state === "suspended") ctx.resume();
    } catch { /* no Web Audio */ }
  };
  window.addEventListener("pointerdown", unlock, { once: true });
  window.addEventListener("keydown", unlock, { once: true });
}

export function playAlertSound(severity) {
  if (!getPrefs().sound || !ctx) return;
  const tones = severity === "critical" ? [880, 660, 880, 660, 880, 660] : [740];
  tones.forEach((freq, i) => {
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    const t0 = ctx.currentTime + i * 0.22;
    osc.type = "sine";
    osc.frequency.value = freq;
    gain.gain.setValueAtTime(0.0001, t0);
    gain.gain.exponentialRampToValueAtTime(0.25, t0 + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.18);
    osc.connect(gain).connect(ctx.destination);
    osc.start(t0);
    osc.stop(t0 + 0.2);
  });
}
