// System health at a glance: what you'd otherwise check in the RabbitMQ UI or the logs.
import type { SystemHealth } from "../api/types";
import { fmtAge, fmtNum } from "../format";

type Tone = "good" | "warn" | "bad" | "";

function Stat({ label, value, sub, tone = "" }: { label: string; value: string; sub?: string; tone?: Tone }) {
  return (
    <div className={`stat ${tone}`}>
      <div className="stat-label">{tone && <span className="dot" />}{label}</div>
      <div className="stat-value num">{value}</div>
      {sub && <div className="stat-sub" title={sub}>{sub}</div>}
    </div>
  );
}

export function SystemPanel({ system, backlog, running }: { system: SystemHealth; backlog: number[]; running: boolean }) {
  const q = system.events_queue;
  const dead = system.dead_letter_queue;
  const workers = system.workers;
  const lag = system.last_event_age_seconds;
  const workersTone: Tone = workers == null ? "bad" : workers === 0 ? "bad" : "good";
  const backlogTone: Tone = !q ? "" : q.messages > 500 ? "bad" : q.messages > 50 ? "warn" : "good";
  const lagTone: Tone = !running || lag == null ? "" : lag > 10 ? "bad" : lag > 3 ? "warn" : "good";
  const simTone: Tone = system.simulator_connected == null ? "" : system.simulator_connected ? "good" : running ? "bad" : "";
  const schedTone: Tone = system.scheduler_age_seconds == null ? (running ? "warn" : "")
    : system.scheduler_age_seconds > 30 && running ? "warn" : "good";

  return (
    <section className="panel">
      <div className="panel-head">
        <h2>System health</h2>
        <span className="hint">RabbitMQ, workers, simulator and scheduler</span>
      </div>
      <div className="panel-body">
        {!system.broker_ok && (
          <div className="banner bad" style={{ marginBottom: 12 }}>
            RabbitMQ unreachable: {system.broker_error ?? "unknown error"}
          </div>
        )}
        <div className="health">
          <Stat label="Workers" tone={workersTone} value={workers == null ? "?" : String(workers)}
                sub={workers === 0 ? "nothing is consuming events" : "consuming transit.events.process"} />
          <Stat label="Queue backlog" tone={backlogTone} value={q ? fmtNum(q.messages) : "–"}
                sub="events waiting for a worker" />
          <Stat label="Dead letters" tone={dead && dead.messages > 0 ? "bad" : dead ? "good" : ""}
                value={dead ? fmtNum(dead.messages) : "–"} sub="rejected (poison) messages" />
          <Stat label="Throughput" value={`${system.events_per_second.toFixed(0)}/s`}
                sub={`${fmtNum(system.events_processed)} events this run`} />
          <Stat label="Last event stored" tone={lagTone} value={fmtAge(lag)} sub="worker → database lag" />
          <Stat label="Simulator" tone={simTone}
                value={system.simulator_connected == null ? "?" : system.simulator_connected ? "Running" : "Stopped"}
                sub={system.simulator_connected ? "following the database schedule" : "not connected"} />
          <Stat label="Scheduler" tone={schedTone} value={fmtAge(system.scheduler_age_seconds)}
                sub={system.auto_approve_hint ? "auto-approve on" : "operator approves changes"} />
        </div>
        <Backlog values={backlog} />
      </div>
    </section>
  );
}

function Backlog({ values }: { values: number[] }) {
  const w = 600, h = 64, pad = 4;
  const max = Math.max(50, ...values);
  const n = Math.max(values.length, 2);
  const x = (i: number) => (i / (n - 1)) * w;
  const y = (v: number) => h - pad - (v / max) * (h - pad * 2);
  const line = values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const area = values.length > 1 ? `${line} L${x(values.length - 1)},${h} L0,${h} Z` : "";
  const last = values[values.length - 1] ?? 0;
  return (
    <div className="backlog">
      <div className="stat-label" style={{ justifyContent: "space-between" }}>
        <span>Queue backlog, last {Math.round((values.length * 1.5) / 60) || 1} min</span>
        <span className="num">peak {fmtNum(Math.max(0, ...values))} · now {fmtNum(last)}</span>
      </div>
      <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" role="img" aria-label="queue backlog over time">
        <line x1="0" x2={w} y1={h - pad} y2={h - pad} stroke="var(--border)" />
        {area && <path d={area} fill="var(--accent)" opacity="0.12" />}
        {line && <path d={line} fill="none" stroke="var(--accent)" strokeWidth="2" vectorEffect="non-scaling-stroke" />}
      </svg>
    </div>
  );
}
