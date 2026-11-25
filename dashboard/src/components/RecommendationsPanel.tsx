// The operator's side of the control loop: pending suggestions with Approve / Decline, and a
// history of past decisions.
import { useState } from "react";
import type { Recommendation } from "../api/types";
import { LABEL_TEXT, labelClass, routeColor } from "../format";

const STATUS: Record<string, { text: string; tone: string }> = {
  pending: { text: "Pending", tone: "accent" },
  approved: { text: "Approved", tone: "good" },
  auto_approved: { text: "Auto-approved", tone: "good" },
  applied: { text: "In effect", tone: "good" },
  declined: { text: "Declined", tone: "" },
  superseded: { text: "Replaced", tone: "" },
};

function busesText(r: Recommendation): string | null {
  if (r.buses_delta === 0) return null;
  const n = Math.abs(r.buses_delta);
  const buses = `${n} bus${n > 1 ? "es" : ""}`;
  if (r.buses_delta < 0) return `release ${buses} to reserve`;
  return r.reallocate_from_route_id ? `move ${buses} from ${r.reallocate_from_route_id}` : `add ${buses} from reserve`;
}

interface Props {
  pending: Recommendation[];
  history: Recommendation[];
  autoApprove: boolean;
  onDecide: (id: number, approve: boolean) => Promise<void>;
}

export function RecommendationsPanel({ pending, history, autoApprove, onDecide }: Props) {
  const [busy, setBusy] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function decide(id: number, approve: boolean) {
    setBusy(id);
    setError(null);
    try {
      await onDecide(id, approve);
    } catch (err) {
      const message = (err as Error).message;
      setError(/superseded|not pending/.test(message)
        ? "That suggestion was replaced by a newer one just before you clicked. Check the updated card."
        : message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Recommendations</h2>
        {pending.length > 0 && <span className="chip accent">{pending.length} pending</span>}
        <span className="spacer" />
        {autoApprove && <span className="hint">auto-approve is on</span>}
      </div>
      <div className="panel-body">
        {error && <div className="banner bad">{error}</div>}
        {pending.length === 0 && (
          <div className="empty">
            {autoApprove ? "The scheduler is applying changes automatically." : "No suggestions waiting. The scheduler checks every 5 simulated minutes."}
          </div>
        )}
        {/* oldest first: new suggestions are added at the bottom, so cards never shift under the cursor */}
        {[...pending].sort((a, b) => a.id - b.id).map((r) => {
          const shorter = r.suggested_interval < r.current_interval;
          const buses = busesText(r);
          return (
            <article className="rec" key={r.id}>
              <div className="rec-top">
                <span className="route-tag"><i className="swatch" style={{ background: routeColor(r.route_id) }} />{r.route_id}</span>
                <span className={`chip ${labelClass(r.peak_label)}`}>{LABEL_TEXT[r.peak_label] ?? r.peak_label}</span>
                <span className="spacer" />
                <span className="faint num" style={{ fontSize: 12 }}>at {r.created_clock ?? `min ${r.created_minute.toFixed(0)}`}</span>
              </div>
              <div className="rec-change num">
                {r.current_interval.toFixed(1)}<span className="arrow">→</span>{r.suggested_interval.toFixed(1)} min
              </div>
              <div className="muted" style={{ fontSize: 13 }}>
                {shorter ? "More frequent" : "Less frequent"}{buses ? ` · ${buses}` : ""} ·{" "}
                <span style={{ color: r.est_wait_saving_minutes > 0 ? "var(--good)" : "var(--muted)" }}>
                  {r.est_wait_saving_minutes > 0
                    ? `saves ~${r.est_wait_saving_minutes.toFixed(1)} min per passenger`
                    : `adds ~${Math.abs(r.est_wait_saving_minutes).toFixed(1)} min wait`}
                </span>
              </div>
              <p className="rec-reason">{r.reason}</p>
              {r.warning && <div className="rec-warning">⚠ {r.warning}</div>}
              <div className="rec-actions">
                <button className="btn primary" disabled={busy !== null} onClick={() => decide(r.id, true)}>
                  {busy === r.id ? "Saving…" : "Approve"}
                </button>
                <button className="btn" disabled={busy !== null} onClick={() => decide(r.id, false)}>Decline</button>
              </div>
            </article>
          );
        })}

        <h3 style={{ fontSize: 13, margin: "20px 0 6px", fontWeight: 650 }}>History</h3>
        {history.length === 0 ? <div className="empty">No decisions yet this run.</div> : (
          <ul className="history">
            {history.map((r) => {
              const s = STATUS[r.status] ?? { text: r.status, tone: "" };
              return (
                <li key={r.id}>
                  <span className="faint num">{r.created_clock ?? ""}</span>
                  <span className="route-tag" style={{ color: routeColor(r.route_id) }}>{r.route_id}</span>
                  <span className="num">
                    {r.current_interval.toFixed(1)} → {r.suggested_interval.toFixed(1)} min
                    {r.buses_delta !== 0 && <span className="faint"> · {r.buses_delta > 0 ? "+" : ""}{r.buses_delta} bus</span>}
                    {r.decided_by && r.decided_by !== "auto" && <span className="faint"> · {r.decided_by}</span>}
                  </span>
                  <span className={`chip ${s.tone}`}>{s.text}</span>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </section>
  );
}
