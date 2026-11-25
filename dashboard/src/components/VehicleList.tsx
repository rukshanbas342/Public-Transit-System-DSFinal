// Every bus: where it is, how full, and how fresh the data is.
import type { StopStatus, VehicleStatus } from "../api/types";
import { fmtAge, loadColor, routeColor } from "../format";

const STATUS_TEXT: Record<string, string> = {
  idle: "Idle", in_service: "Driving", at_stop: "At stop", broken_down: "Broken down", deadhead: "Driving empty",
};

export function VehicleList({ vehicles, stops, lagging }: { vehicles: VehicleStatus[]; stops: StopStatus[]; lagging: boolean }) {
  const stopName = new Map(stops.map((s) => [s.stop_id, s.name]));
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Buses</h2>
        <span className="hint">{lagging ? "greyed out: the worker isn't storing events, so this may be out of date" : "a bus reports at each stop, so a few seconds between reports is normal"}</span>
      </div>
      <div className="panel-body table-wrap">
        <table>
          <thead>
            <tr><th>Bus</th><th>Route</th><th>Status</th><th>Last stop</th><th>On board</th><th className="r">Last reported</th></tr>
          </thead>
          <tbody>
            {vehicles.map((v) => {
              const ratio = v.capacity ? v.occupancy / v.capacity : 0;
              const stale = lagging && v.route_id != null;
              return (
                <tr key={v.vehicle_id} className={stale ? "stale" : ""}>
                  <td className="num" style={{ fontWeight: 600 }}>{v.vehicle_id}</td>
                  <td>{v.route_id
                    ? <span className="route-tag"><i className="swatch" style={{ background: routeColor(v.route_id) }} />{v.route_id}</span>
                    : <span className="faint">reserve</span>}</td>
                  <td>{v.status === "broken_down"
                    ? <span className="chip bad">Broken down</span>
                    : STATUS_TEXT[v.status] ?? v.status}</td>
                  <td>{v.stop_id ? stopName.get(v.stop_id) ?? v.stop_id : <span className="faint">–</span>}</td>
                  <td className="num">
                    <span className="bar"><i style={{ width: `${Math.min(100, ratio * 100)}%`, background: loadColor(ratio) }} /></span>
                    <span style={{ marginLeft: 8 }}>{v.occupancy}/{v.capacity}</span>
                  </td>
                  <td className="r num">{v.route_id ? fmtAge(v.age_seconds) : "–"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
