import { Check } from "lucide-react";
import { Fragment } from "react";

export const EVENT_NAMES = {
  OSL: "Over speed limit",
  ABT: "Accelerating before turn",
  BAT: "Braking after turn",
  FAB: "Frequent acceleration & braking",
  HA: "Harsh acceleration",
  HB: "Harsh braking",
  HT: "Harsh turn",
};

export function band(score) {
  if (score == null) return "None";
  if (score >= 100) return "Excellent";
  if (score >= 85) return "Good";
  if (score >= 75) return "Fair";
  return "Okay";
}

/** Speed-limit-sign panel: used for the posted limit and for driving scores. */
export function Sign({ label, value, size = "", stripe, over = false, title }) {
  return (
    <span className={`sign ${size} ${over ? "over" : ""}`} title={title} role="img"
          aria-label={`${label} ${value ?? "not available"}`}>
      <span className="sign-label">{label}</span>
      <span className="sign-value">{value ?? "-"}</span>
      {stripe && <span className={`band-stripe band-${stripe}`} />}
    </span>
  );
}

export const ScoreSign = ({ score, size = "" }) => (
  <Sign label="Score" value={score ?? "-"} size={size} stripe={score == null ? undefined : band(score)} />
);

export function Pips({ level }) {
  if (!level) return <span className="ok-tick" aria-label="no events"><Check size={18} strokeWidth={3} /></span>;
  return (
    <span className="pips" aria-label={`level ${level} of 3`}>
      {[1, 2, 3].map((i) => <i key={i} className={i <= level ? "on" : ""} />)}
    </span>
  );
}

/** Semicircle score gauge for the driving-analysis tab. */
export function Gauge({ score }) {
  const r = 80;
  const len = Math.PI * r;
  const pct = score == null ? 0 : Math.max(0, Math.min(100, score)) / 100;
  const color = score == null ? "var(--line)" : score >= 85 ? "var(--green)" : score >= 75 ? "#d9a800" : "var(--red)";
  return (
    <svg viewBox="0 0 200 116" width="210" role="img" aria-label={`Score ${score ?? "not available"} of 100`}>
      <path d="M20 100 A80 80 0 0 1 180 100" fill="none" stroke="var(--concrete)" strokeWidth="16" strokeLinecap="round" />
      <path d="M20 100 A80 80 0 0 1 180 100" fill="none" stroke={color} strokeWidth="16" strokeLinecap="round"
            strokeDasharray={`${len * pct} ${len}`} style={{ transition: "stroke-dasharray .8s ease" }} />
      <text x="100" y="92" textAnchor="middle" fontFamily="var(--font-num)" fontWeight="700" fontSize="50" fill="var(--ink)">
        {score ?? "-"}
      </text>
      <text x="100" y="112" textAnchor="middle" fontSize="12" fill="var(--ink-soft)">{band(score)}</text>
    </svg>
  );
}

/** Where a sensor value sits relative to its healthy band and limits. */
export function RangeBar({ value, band: [lo, hi], warn, crit, direction }) {
  const min = direction === "up" ? lo - (hi - lo) * 0.3 : crit - (hi - crit) * 0.25;
  const max = direction === "up" ? crit + (crit - hi) * 0.4 : hi + (hi - lo) * 0.35;
  const pos = (v) => `${Math.max(0, Math.min(100, ((v - min) / (max - min)) * 100))}%`;
  const okStart = direction === "up" ? min : lo;
  const okEnd = direction === "up" ? hi : max;
  return (
    <div className="range-bar" aria-hidden="true">
      {direction === "up" ? (
        <span className="seg warn" style={{ left: pos(hi), width: `calc(${pos(crit)} - ${pos(hi)})` }} />
      ) : (
        <span className="seg warn" style={{ left: pos(crit), width: `calc(${pos(lo)} - ${pos(crit)})` }} />
      )}
      <span className="seg ok" style={{ left: pos(okStart), width: `calc(${pos(okEnd)} - ${pos(okStart)})` }} />
      {value != null && <span className="marker" style={{ left: pos(value) }} />}
      <span className="sr-only">warning at {warn}</span>
    </div>
  );
}

/** Minimal, safe markdown for AI answers: ### headings, lists, **bold**, _italic_, [S1] citations. */
export function Markdown({ text }) {
  const blocks = [];
  let list = null;
  const inline = (s, key) => {
    const parts = s.split(/(\*\*[^*]+\*\*|\[S\d+\]|_[^_]+_)/g);
    return (
      <Fragment key={key}>
        {parts.map((p, i) => {
          if (/^\*\*[^*]+\*\*$/.test(p)) return <strong key={i}>{p.slice(2, -2)}</strong>;
          if (/^\[S\d+\]$/.test(p)) return <span key={i} className="cite">{p.slice(1, -1)}</span>;
          if (/^_[^_]+_$/.test(p)) return <em key={i} className="muted">{p.slice(1, -1)}</em>;
          return p;
        })}
      </Fragment>
    );
  };
  (text || "").split("\n").forEach((raw, i) => {
    const line = raw.trim();
    const item = line.match(/^(?:[-*]|\d+\.)\s+(.*)$/);
    if (item) {
      if (!list) {
        list = { ordered: /^\d+\./.test(line), items: [] };
        blocks.push(list);
      }
      list.items.push(inline(item[1], i));
      return;
    }
    list = null;
    if (!line) return;
    const h = line.match(/^#{1,4}\s+(.*)$/);
    blocks.push(h ? <h4 key={i}>{inline(h[1], i)}</h4> : <p key={i}>{inline(line, i)}</p>);
  });
  return (
    <div className="md">
      {blocks.map((b, i) => {
        if (b && b.items) {
          const Tag = b.ordered ? "ol" : "ul";
          return <Tag key={`l${i}`}>{b.items.map((it, j) => <li key={j}>{it}</li>)}</Tag>;
        }
        return b;
      })}
    </div>
  );
}

export function Sources({ sources }) {
  if (!sources?.length) return null;
  return (
    <div className="sources" aria-label="Sources">
      {sources.map((s) => (
        <span key={s.tag} className="source" title={`${s.title} (${s.via?.join(" + ")} search)`}>
          <b>{s.tag}</b>{s.section}
        </span>
      ))}
    </div>
  );
}

export function ModelBadge({ result }) {
  if (!result) return null;
  return (
    <span className={`model-badge ${result.used_llm ? "live" : ""}`}
          title={result.llm_error || (result.cached ? "Saved answer" : `${result.latency_ms} ms`)}>
      {result.used_llm ? result.model : "Offline template"}
    </span>
  );
}

export function fmtDuration(s) {
  if (s == null) return "-";
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = Math.floor(s % 60);
  if (h) return `${h} h ${String(m).padStart(2, "0")} min`;
  return `${m} min ${String(sec).padStart(2, "0")} s`;
}

export function fmtAgo(ts, now = Date.now()) {
  if (!ts) return "never";
  const s = Math.max(0, Math.round((now - new Date(ts).getTime()) / 1000));
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

export const fmtTime = (ts) => new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
export const fmtDate = (ts) => new Date(ts).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
export const fmtNum = (n, d = 0) => (n == null ? "-" : Number(n).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d }));

/** Accessible on/off switch (a real checkbox underneath). */
export function Toggle({ checked, onChange, disabled = false, label }) {
  return (
    <span className="toggle">
      <input type="checkbox" role="switch" aria-label={label} checked={!!checked} disabled={disabled}
             onChange={(e) => onChange?.(e.target.checked)} />
      <span />
    </span>
  );
}
