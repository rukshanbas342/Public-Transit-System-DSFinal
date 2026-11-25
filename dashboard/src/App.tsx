// Top-level layout and polling loop: login gate, then the live operator view refreshed every
// POLL_MS from /api/overview.
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api/client";
import type { Overview } from "./api/types";
import { BusIcon, Login } from "./components/Login";
import { NetworkMap } from "./components/NetworkMap";
import { RecommendationsPanel } from "./components/RecommendationsPanel";
import { RouteStatus } from "./components/RouteStatus";
import { SystemPanel } from "./components/SystemPanel";
import { VehicleList } from "./components/VehicleList";
import { fmtMin, fmtNum, isLagging } from "./format";

const POLL_MS = 1500;
const BACKLOG_SAMPLES = 160;     // ~4 minutes of history for the backlog chart
const TOKEN_KEY = "transit.token";
const USER_KEY = "transit.user";

function readSession(key: string): string | null {
  try { return sessionStorage.getItem(key); } catch { return null; }
}
function writeSession(key: string, value: string | null) {
  try { value == null ? sessionStorage.removeItem(key) : sessionStorage.setItem(key, value); } catch { /* private mode */ }
}

export default function App() {
  const [token, setToken] = useState<string | null>(() => readSession(TOKEN_KEY));
  const [user, setUser] = useState<string | null>(() => readSession(USER_KEY));
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastOk, setLastOk] = useState<number | null>(null);
  const backlog = useRef<number[]>([]);
  const runId = useRef<string | null>(null);

  const logout = useCallback(() => {
    writeSession(TOKEN_KEY, null);
    writeSession(USER_KEY, null);
    setToken(null);
    setUser(null);
  }, []);

  const refresh = useCallback(async () => {
    try {
      const next = await api.overview();
      if (next.run?.run_id !== runId.current) {      // new run: start the chart over
        runId.current = next.run?.run_id ?? null;
        backlog.current = [];
      }
      backlog.current = [...backlog.current, next.system.events_queue?.messages ?? 0].slice(-BACKLOG_SAMPLES);
      setData(next);
      setError(null);
      setLastOk(Date.now());
    } catch (err) {
      setError((err as Error).message);
    }
  }, []);

  useEffect(() => {
    if (!token) return;
    api.me(token).catch((err) => { if (err instanceof ApiError && err.status === 401) logout(); });
    refresh();
    const timer = window.setInterval(refresh, POLL_MS);
    return () => window.clearInterval(timer);
  }, [token, refresh, logout]);

  const decide = useCallback(async (id: number, approve: boolean) => {
    if (!token) return;
    try {
      await (approve ? api.approve(id, token) : api.decline(id, token));
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) logout();
      throw err;
    } finally {
      refresh();
    }
  }, [token, refresh, logout]);

  if (!token) {
    return <Login onLogin={(t, u) => { writeSession(TOKEN_KEY, t); writeSession(USER_KEY, u); setToken(t); setUser(u); }} />;
  }

  const run = data?.run ?? null;
  const running = !!run && !run.finished_at;
  const progress = run && run.sim_minute != null && run.duration_minutes ? Math.min(1, run.sim_minute / run.duration_minutes) : 0;
  const lagging = !!data && isLagging(data.system.last_event_age_seconds, running);
  const offline = error != null && (lastOk == null || Date.now() - lastOk > 5000);

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark"><BusIcon /></span>
          Transit Operations
        </div>
        {run && (
          <div className="run-meta">
            <span className="clock num">{run.clock ?? run.service_start}</span>
            <span className={`chip ${running ? "good" : ""}`}>
              <span className={`dot ${running ? "pulse" : ""}`} />{running ? "Running" : "Finished"}
            </span>
            <span className="chip" title={run.run_id}>{run.scenario} · seed {run.seed}</span>
            <span className="faint num" style={{ fontSize: 12 }}>
              {run.service_start}–{run.service_end} · {Math.round(progress * 100)}% of the day
            </span>
          </div>
        )}
        <span className="spacer" />
        <span className="muted" style={{ fontSize: 13 }}>{user}</span>
        <button className="btn ghost small" onClick={logout}>Sign out</button>
      </header>

      {offline && <div className="banner bad">Lost contact with the API ({error}). Retrying every {POLL_MS / 1000} s…</div>}
      {data && !run && (
        <div className="banner info">No simulation run yet. Start the simulator (docker compose up) and this page fills in automatically.</div>
      )}
      {!data && !error && <div className="empty">Loading…</div>}

      {data && (
        <div className="stack">
          {data.totals && (
            <div className="kpis">
              <Kpi label="Passengers arrived" value={fmtNum(data.totals.arrived)} />
              <Kpi label="Served" value={fmtNum(data.totals.served)} />
              <Kpi label="Waiting now" value={fmtNum(data.totals.waiting)} />
              <Kpi label="Average wait" value={fmtMin(data.totals.avg_wait)} />
              <Kpi label="95th percentile wait" value={fmtMin(data.totals.p95_wait)} />
              <Kpi label="Left behind (bus full)" value={fmtNum(data.totals.left_behind)} warn={data.totals.left_behind > 0} />
            </div>
          )}
          <SystemPanel system={data.system} backlog={backlog.current} running={running} />
          <div className="grid grid-main">
            <div className="stack">
              <NetworkMap routes={data.routes} stops={data.stops} vehicles={data.vehicles}
                          lagging={lagging} lagSeconds={data.system.last_event_age_seconds} />
              <RouteStatus routes={data.routes} simMinute={run?.sim_minute ?? null} />
              <VehicleList vehicles={data.vehicles} stops={data.stops} lagging={lagging} />
            </div>
            <RecommendationsPanel pending={data.pending} history={data.history}
                                  autoApprove={data.system.auto_approve_hint} onDecide={decide} />
          </div>
        </div>
      )}
    </div>
  );
}

function Kpi({ label, value, warn }: { label: string; value: string; warn?: boolean }) {
  return (
    <div className="stat" style={{ background: "var(--panel)" }}>
      <div className="stat-label">{label}</div>
      <div className="stat-value num" style={warn ? { color: "var(--warn)" } : undefined}>{value}</div>
    </div>
  );
}
