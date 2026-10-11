// Top-down vehicle diagram, the way an in-car tyre-pressure screen shows it.
// Tyres turn yellow/red when the health engine flags them; headlights show when on.
const TIRES = [
  { key: "fl", x: 84, y: 66, label: "Front left", lx: 74, anchor: "end" },
  { key: "fr", x: 176, y: 66, label: "Front right", lx: 206, anchor: "start" },
  { key: "rl", x: 84, y: 214, label: "Rear left", lx: 74, anchor: "end" },
  { key: "rr", x: 176, y: 214, label: "Rear right", lx: 206, anchor: "start" },
];

export default function CarSchematic({ state }) {
  const tires = state?.tires || {};
  const signals = state?.health?.signals || {};
  const color = (key) => {
    const st = signals[`tire_${key}`]?.status;
    return st === "critical" ? "var(--red)" : st === "watch" || st === "warning" ? "#d9a800" : "var(--asphalt)";
  };
  return (
    <svg viewBox="0 0 280 300" width="280" height="300" role="img" style={{ maxWidth: "100%" }}
         aria-label={`Tyre pressures: ${TIRES.map((t) => `${t.label} ${tires[t.key] ?? "unknown"} psi`).join(", ")}`}>
      {state?.headlights && (
        <g fill="var(--yellow)" opacity="0.35">
          <path d="M110 34 L92 0 L132 0 L124 34 Z" />
          <path d="M170 34 L188 0 L148 0 L156 34 Z" />
        </g>
      )}
      {/* body */}
      <rect x="92" y="30" width="96" height="236" rx="34" fill="var(--paper)" stroke="var(--asphalt)" strokeWidth="3" />
      <path d="M102 96 Q140 80 178 96 L172 126 Q140 116 108 126 Z" fill="var(--concrete)" stroke="var(--asphalt)" strokeWidth="2" />
      <path d="M108 206 Q140 216 172 206 L176 228 Q140 240 104 228 Z" fill="var(--concrete)" stroke="var(--asphalt)" strokeWidth="2" />
      <line x1="98" y1="150" x2="182" y2="150" stroke="var(--line)" strokeWidth="2" />
      <text x="140" y="168" textAnchor="middle" fontSize="11" fill="var(--ink-soft)">
        {state ? (state.doors_locked ? "Locked" : "Unlocked") : ""}
      </text>
      {/* tyres + pressures */}
      {TIRES.map((t) => {
        const c = color(t.key);
        return (
          <g key={t.key}>
            <rect x={t.x} y={t.y} width="20" height="40" rx="6" fill={c} />
            <text x={t.lx} y={t.y + 20} textAnchor={t.anchor} fontFamily="var(--font-num)" fontWeight="600"
                  fontSize="24" fill={c === "var(--asphalt)" ? "var(--ink)" : c}>
              {tires[t.key] != null ? tires[t.key].toFixed(1) : "-"}
            </text>
            <text x={t.lx} y={t.y + 35} textAnchor={t.anchor} fontSize="10.5" fill="var(--ink-soft)">psi</text>
          </g>
        );
      })}
    </svg>
  );
}
