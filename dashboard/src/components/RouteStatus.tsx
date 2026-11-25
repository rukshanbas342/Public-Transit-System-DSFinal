// Per-route summary: schedule, demand label, waits and passengers left behind.
import type { RouteStatus as Route } from "../api/types";
import { fmtMin, fmtNum, LABEL_TEXT, labelClass, routeColor } from "../format";

export function RouteStatus({ routes, simMinute }: { routes: Route[]; simMinute: number | null }) {
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Routes</h2>
        <span className="hint">waits are minutes from arriving at the stop to boarding</span>
      </div>
      <div className="panel-body table-wrap">
        <table>
          <thead>
            <tr>
              <th>Route</th><th>Demand</th><th className="r">Interval</th><th className="r">Buses</th>
              <th className="r">Waiting</th><th className="r">Avg wait</th><th className="r">Last hour</th>
              <th className="r">p95 wait</th><th className="r">Left behind</th><th className="r">Served</th>
              <th>Schedule</th>
            </tr>
          </thead>
          <tbody>
            {routes.map((r) => {
              const applied = r.applied_version != null && r.applied_version >= r.schedule_version;
              const cooling = r.cooldown_until_minute != null && simMinute != null && simMinute < r.cooldown_until_minute;
              return (
                <tr key={r.route_id}>
                  <td><span className="route-tag"><i className="swatch" style={{ background: routeColor(r.route_id) }} />{r.route_id}</span></td>
                  <td>
                    <span className={`chip ${labelClass(r.peak_label)}`}>{LABEL_TEXT[r.peak_label] ?? r.peak_label}</span>
                    <span className="faint num" style={{ marginLeft: 6 }}>{r.demand_score?.toFixed(2) ?? ""}</span>
                  </td>
                  <td className="r num">{fmtMin(r.interval_minutes)}</td>
                  <td className="r num">{r.buses_on_route}/{r.buses_assigned}</td>
                  <td className="r num">{fmtNum(r.waiting)}</td>
                  <td className="r num">{fmtMin(r.avg_wait)}</td>
                  <td className="r num">{fmtMin(r.recent_avg_wait)}</td>
                  <td className="r num">{fmtMin(r.p95_wait)}</td>
                  <td className="r num" style={{ color: r.left_behind ? "var(--warn)" : undefined }}>{fmtNum(r.left_behind)}</td>
                  <td className="r num">{fmtNum(r.served)}</td>
                  <td>
                    <span className="num">v{r.schedule_version}</span>{" "}
                    {applied ? <span className="chip good">in effect</span>
                      : <span className="chip accent" title="approved; the simulator switches at the route's next departure">waiting for next departure</span>}
                    {cooling && <span className="chip" style={{ marginLeft: 4 }} title="no new suggestions for one round trip after a change">cooldown</span>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
