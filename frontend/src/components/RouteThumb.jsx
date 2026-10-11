// Tiny route drawing for trip lists - an SVG polyline, no map tiles (cheap to render 40 of them).
export default function RouteThumb({ points, width = 76, height = 58 }) {
  if (!points || points.length < 2) return <div className="route-thumb" />;
  const lats = points.map((p) => p[0]);
  const lons = points.map((p) => p[1]);
  const [minLat, maxLat, minLon, maxLon] = [Math.min(...lats), Math.max(...lats), Math.min(...lons), Math.max(...lons)];
  const pad = 8;
  const kx = Math.cos((((minLat + maxLat) / 2) * Math.PI) / 180);  // keep the shape's aspect ratio
  const spanX = (maxLon - minLon) * kx || 1e-6;
  const spanY = maxLat - minLat || 1e-6;
  const scale = Math.min((width - pad * 2) / spanX, (height - pad * 2) / spanY);
  const offX = (width - spanX * scale) / 2;
  const offY = (height - spanY * scale) / 2;
  const xy = points.map(([la, lo]) => [offX + (lo - minLon) * kx * scale, height - (offY + (la - minLat) * scale)]);
  const d = xy.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`).join(" ");
  const [sx, sy] = xy[0];
  const [ex, ey] = xy[xy.length - 1];
  return (
    <svg className="route-thumb" viewBox={`0 0 ${width} ${height}`} width={width} height={height} aria-hidden="true">
      <path d={d} fill="none" stroke="var(--green)" strokeWidth="2.5" strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={sx} cy={sy} r="3.5" fill="var(--paper)" stroke="var(--green)" strokeWidth="2" />
      <circle cx={ex} cy={ey} r="3.5" fill="var(--red)" />
    </svg>
  );
}
