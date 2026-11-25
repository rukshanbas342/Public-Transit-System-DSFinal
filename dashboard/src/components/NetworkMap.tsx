// Visual network: each route drawn as a loop of stops. Buses sit at their current stop (or
// half-way to the next one when driving), coloured by occupancy. Waiting passengers are shown
// per stop. When the worker stops storing events, every bus is drawn hollow (positions are out
// of date), so a stopped worker is visible straight away.
import type { RouteStatus, StopStatus, VehicleStatus } from "../api/types";
import { fmtAge, LABEL_TEXT, labelClass, loadColor, routeColor } from "../format";

const W = 340, H = 190, M = 34;            // drawing size and margin around the loop
const PERIM = 2 * (W - 2 * M) + 2 * (H - 2 * M);

/** Point at fraction t (0..1) around the loop, clockwise from the top-left (the terminal). */
function pointAt(t: number): [number, number] {
  const w = W - 2 * M, h = H - 2 * M;
  let d = (((t % 1) + 1) % 1) * PERIM;
  if (d < w) return [M + d, M];
  d -= w;
  if (d < h) return [M + w, M + d];
  d -= h;
  if (d < w) return [M + w - d, M + h];
  d -= w;
  return [M, M + h - d];
}

/** Unit vector pointing outward from the loop at fraction t (for placing labels and buses). */
function outward(t: number): [number, number] {
  const [x, y] = pointAt(t);
  const cx = W / 2, cy = H / 2;
  const dx = x - cx, dy = y - cy;
  // Push along the dominant axis so labels clear the track on straight edges.
  if (Math.abs(dx) / (W - 2 * M) > Math.abs(dy) / (H - 2 * M)) return [Math.sign(dx), 0];
  return [0, Math.sign(dy)];
}

function busFraction(v: VehicleStatus, n: number): number {
  const i = v.stop_index ?? 0;
  if (v.status === "in_service" && v.stop_index != null) return (i + 0.5) / n;
  return i / n;
}

function RouteLoop({ route, stops, buses, stale }: { route: RouteStatus; stops: StopStatus[]; buses: VehicleStatus[]; stale: boolean }) {
  const n = Math.max(stops.length, 1);
  const color = routeColor(route.route_id);
  const maxWaiting = Math.max(10, ...stops.map((s) => s.waiting));

  // Group buses that share a position so they stack instead of overlapping.
  const groups = new Map<string, VehicleStatus[]>();
  for (const b of buses) {
    const key = busFraction(b, n).toFixed(3);
    groups.set(key, [...(groups.get(key) ?? []), b]);
  }

  return (
    <div className="route-card">
      <h3>
        <span className="swatch" style={{ background: color }} />
        {route.route_id} · {route.name.replace(/^Route [A-Z] - /, "")}
        <span className={`chip ${labelClass(route.peak_label)}`} style={{ marginLeft: "auto" }}>
          {LABEL_TEXT[route.peak_label] ?? route.peak_label}
        </span>
      </h3>
      <div className="muted num" style={{ fontSize: 12, marginTop: 2 }}>
        every {route.interval_minutes.toFixed(1)} min · {route.buses_on_route}/{route.buses_assigned} buses · {route.waiting} waiting
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${route.route_id} network`}>
        <rect x={M} y={M} width={W - 2 * M} height={H - 2 * M} rx="18" fill="none"
              stroke="var(--track)" strokeWidth="6" />
        <rect x={M} y={M} width={W - 2 * M} height={H - 2 * M} rx="18" fill="none"
              stroke={color} strokeWidth="2" opacity="0.6" />
        {stops.map((s, i) => {
          const t = i / n;
          const [x, y] = pointAt(t);
          const [ox, oy] = outward(t);
          const r = 3 + 9 * Math.sqrt(s.waiting / maxWaiting);
          const terminal = i === 0;
          return (
            <g key={s.stop_id}>
              <title>{`${s.name}: ${s.waiting} waiting`}</title>
              {s.waiting > 0 && (
                <circle cx={x} cy={y} r={r + 3} fill={s.waiting > 25 ? "var(--bad)" : "var(--warn)"} opacity="0.18" />
              )}
              <circle cx={x} cy={y} r={terminal ? 6 : 4.5} fill="var(--panel)" stroke={color} strokeWidth={terminal ? 3 : 2} />
              {s.waiting > 0 && (
                // waiting count sits outside the loop, buses sit inside it
                <text x={x + ox * 18} y={y + oy * 18 + 3.5}
                      textAnchor="middle" fontSize="10" fontWeight="600"
                      fill={s.waiting > 25 ? "var(--bad)" : "var(--muted)"} className="num">
                  {s.waiting}
                </text>
              )}
              {terminal && (
                <text x={x - 9} y={y + 3} textAnchor="end" fontSize="9" fill="var(--faint)">start</text>
              )}
            </g>
          );
        })}
        {[...groups.entries()].map(([key, group]) => {
          const t = parseFloat(key);
          const [x, y] = pointAt(t);
          const [ox, oy] = outward(t);
          return group.map((b, k) => {
            // stack inward, away from the stop labels
            const bx = x - ox * (16 + k * 15), by = y - oy * (16 + k * 15);
            const ratio = b.capacity ? b.occupancy / b.capacity : 0;
            const fill = b.status === "broken_down" ? "var(--bad)" : loadColor(ratio);
            return (
              <g key={b.vehicle_id}>
                <title>{`${b.vehicle_id} · ${b.status.replace("_", " ")} · ${b.occupancy}/${b.capacity} on board · updated ${fmtAge(b.age_seconds)}`}</title>
                <rect x={bx - 12} y={by - 7} width="24" height="14" rx="4"
                      fill={stale ? "var(--panel)" : fill} stroke={stale ? "var(--faint)" : fill}
                      strokeWidth="1.5" strokeDasharray={stale ? "3 2" : undefined} />
                <text x={bx} y={by + 3.5} textAnchor="middle" fontSize="9" fontWeight="700"
                      fill={stale ? "var(--faint)" : "white"} className="num">
                  {b.status === "broken_down" ? "!" : b.occupancy}
                </text>
              </g>
            );
          });
        })}
      </svg>
    </div>
  );
}

interface Props { routes: RouteStatus[]; stops: StopStatus[]; vehicles: VehicleStatus[]; lagging: boolean; lagSeconds: number | null }

export function NetworkMap({ routes, stops, vehicles, lagging, lagSeconds }: Props) {
  const reserve = vehicles.filter((v) => !v.route_id);
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Network</h2>
        <span className="hint">buses show passengers on board; numbers at stops are people waiting</span>
        <span className="spacer" />
        {lagging && <span className="chip bad">No events stored for {fmtAge(lagSeconds).replace(" ago", "")}: positions out of date</span>}
      </div>
      <div className="panel-body">
        <div className="routes-map">
          {routes.map((r) => (
            <RouteLoop key={r.route_id} route={r}
                       stops={stops.filter((s) => s.route_id === r.route_id).sort((a, b) => a.stop_index - b.stop_index)}
                       buses={vehicles.filter((v) => v.route_id === r.route_id)} stale={lagging} />
          ))}
        </div>
        <div className="legend">
          <span><i className="swatch" style={{ background: "var(--good)" }} /> under 60% full</span>
          <span><i className="swatch" style={{ background: "var(--warn)" }} /> 60–90%</span>
          <span><i className="swatch" style={{ background: "var(--bad)" }} /> 90%+ or broken down</span>
          <span><i className="swatch" style={{ border: "1.5px dashed var(--faint)" }} /> out of date (worker not storing events)</span>
          <span style={{ marginLeft: "auto" }}>Reserve: {reserve.length ? reserve.map((v) => v.vehicle_id).join(", ") : "none"}</span>
        </div>
      </div>
    </section>
  );
}
