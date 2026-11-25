"""Database side of the scheduler.

Each evaluation (every `evaluation.every_minutes` of simulated time):
  1. find the active run (latest sim_runs row, not finished) and the simulated clock
     (latest vehicle_state.last_sim_minute for that run)
  2. per route, read passenger_journeys / stop_visits for the last window
  3. score demand (demand_score.py), update route_scheduler_state (EMA, label, cooldown,
     suppression), and plan all routes together (policy.py)
  4. insert recommendations rows; supersede pending ones that are stale or replaced
  5. with auto-approve, apply them at once (operator_decisions.operator = "auto")

`approve_recommendation` / `decline_recommendation` hold the approval transaction from
CONTEXT.md section 2. The API should call them too, so both paths behave the same.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from transit.common.config import SchedulerConfig, SimulationConfig, load_scheduler_config
from transit.common.models import (
    HistoricalDemand,
    OperatorDecision,
    PassengerJourney,
    Recommendation,
    RouteSchedule,
    RouteSchedulerState,
    SimRun,
    StopVisit,
    VehicleState,
)
from transit.scheduler import demand_score as ds
from transit.scheduler import policy

log = logging.getLogger(__name__)

APPROVED_STATUSES = ("approved", "auto_approved", "applied")


class RecommendationError(Exception):
    """The recommendation can't be approved/declined (wrong status, or the fleet changed)."""


# --------------------------------------------------------------------------- #
# Run lookup helpers
# --------------------------------------------------------------------------- #
def active_run(session: Session) -> SimRun | None:
    return session.scalars(select(SimRun).order_by(SimRun.started_at.desc(), SimRun.run_id.desc())
                           .limit(1)).first()


def current_sim_minute(session: Session, run_id: str) -> float | None:
    vehicle = session.scalar(select(func.max(VehicleState.last_sim_minute))
                             .where(VehicleState.run_id == run_id))
    passenger = session.scalar(select(func.max(PassengerJourney.arrived_minute))
                               .where(PassengerJourney.run_id == run_id))
    known = [m for m in (vehicle, passenger) if m is not None]
    return max(known) if known else None


def run_config(run: SimRun) -> SimulationConfig:
    return SimulationConfig.model_validate(run.config)


def _round_trips(sim_cfg: SimulationConfig) -> dict[str, float]:
    return {r.id: policy.round_trip_minutes(r, sim_cfg.fleet.dwell) for r in sim_cfg.routes}


# --------------------------------------------------------------------------- #
# Approve / decline (shared with the API)
# --------------------------------------------------------------------------- #
def approve_recommendation(
    session: Session,
    rec: Recommendation,
    *,
    operator: str,
    decided_minute: float | None,
    auto: bool = False,
    cfg: SchedulerConfig | None = None,
) -> None:
    """Approve a pending recommendation and update route_schedules (version + 1), in the caller's transaction.

    Raises RecommendationError if it isn't pending or the fleet no longer has the buses.
    """
    if rec.status != "pending":
        raise RecommendationError(f"recommendation {rec.id} is {rec.status}, not pending")
    cfg = cfg or load_scheduler_config()
    run = session.get(SimRun, rec.run_id)
    sim_cfg = run_config(run) if run else None

    ids = sorted({rec.route_id, *([rec.reallocate_from_route_id] if rec.reallocate_from_route_id else [])})
    schedules = {s.route_id: s for s in session.scalars(
        select(RouteSchedule).where(RouteSchedule.route_id.in_(ids)).order_by(RouteSchedule.route_id)
        .with_for_update())}
    target = schedules[rec.route_id]
    new_buses = target.buses_assigned + rec.buses_delta
    if new_buses < 0:
        raise RecommendationError(f"route {rec.route_id} has only {target.buses_assigned} bus(es)")

    donor = schedules.get(rec.reallocate_from_route_id) if rec.reallocate_from_route_id else None
    if donor is not None:
        donor_buses = donor.buses_assigned - rec.buses_delta
        if donor_buses < 1:
            raise RecommendationError(f"donor route {donor.route_id} would be left without buses")
    elif rec.buses_delta > 0 and sim_cfg is not None:
        others = session.scalar(select(func.coalesce(func.sum(RouteSchedule.buses_assigned), 0))
                                .where(RouteSchedule.route_id != rec.route_id))
        if others + new_buses > sim_cfg.fleet.size:
            raise RecommendationError(
                f"not enough reserve buses: {sim_cfg.fleet.size - others - target.buses_assigned} free, "
                f"{rec.buses_delta} needed")

    status = "auto_approved" if auto else "approved"
    source = "auto" if auto else "operator"
    _bump(target, rec.suggested_interval, new_buses, source, rec.id)
    if donor is not None:
        round_trip = _round_trips(sim_cfg)[donor.route_id] if sim_cfg else donor.interval_minutes * donor.buses_assigned
        view = policy.RouteView(donor.route_id, donor.interval_minutes, donor.buses_assigned, round_trip, 0, 0, "")
        _bump(donor, policy.donor_interval_after(view, donor_buses, cfg.interval), donor_buses, source, rec.id)

    # Approved earlier but not applied yet: the simulator only applies the newest version, so
    # those will never be applied. Mark them superseded so they don't look stuck.
    for older in session.scalars(select(Recommendation).where(
            Recommendation.route_id.in_(ids), Recommendation.run_id == rec.run_id,
            Recommendation.status.in_(["approved", "auto_approved"]), Recommendation.id != rec.id)):
        older.status = "superseded"
    rec.status = status
    session.add(OperatorDecision(recommendation_id=rec.id, decision=status, operator=operator,
                                 decided_minute=decided_minute))


def _bump(schedule: RouteSchedule, interval: float, buses: int, source: str, rec_id: int) -> None:
    schedule.interval_minutes = round(interval, 1)
    schedule.buses_assigned = buses
    schedule.version += 1
    schedule.source = source
    schedule.recommendation_id = rec_id


def decline_recommendation(session: Session, rec: Recommendation, *, operator: str,
                           decided_minute: float | None) -> None:
    if rec.status != "pending":
        raise RecommendationError(f"recommendation {rec.id} is {rec.status}, not pending")
    rec.status = "declined"
    session.add(OperatorDecision(recommendation_id=rec.id, decision="declined", operator=operator,
                                 decided_minute=decided_minute))


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #
@dataclass
class EvaluationResult:
    run_id: str
    sim_minute: float
    created: list[int]
    superseded: list[int]


class SchedulerService:
    def __init__(self, cfg: SchedulerConfig, session_factory: Callable[[], Session], auto_approve: bool):
        self.cfg = cfg
        self.session_factory = session_factory
        self.auto_approve = auto_approve
        self._historical: dict[str, dict[int, float]] | None = None

    def tick(self) -> EvaluationResult | None:
        """Evaluate if enough simulated time has passed. One transaction per evaluation."""
        with self.session_factory() as session:
            result = self.evaluate(session)
            session.commit()
            return result

    def evaluate(self, session: Session, force: bool = False) -> EvaluationResult | None:
        run = active_run(session)
        if run is None or run.finished_at is not None:
            return None
        now = current_sim_minute(session, run.run_id)
        if now is None:
            return None
        sim_cfg = run_config(run)
        route_ids = [r.id for r in sim_cfg.routes]

        states = self._load_states(session, run.run_id, route_ids)
        last = [s.last_evaluated_minute for s in states.values() if s.last_evaluated_minute is not None]
        if not force and len(last) == len(states) and now - min(last) < self.cfg.evaluation.every_minutes:
            return None

        metrics = self._window_metrics(session, run.run_id, route_ids, now)
        historical = self._historical_demand(session)
        round_trips = _round_trips(sim_cfg)
        schedules = {s.route_id: s for s in session.scalars(select(RouteSchedule))}
        clock = sim_cfg.time.start_minute + now
        hour = int(clock // 60) % 24
        decided = self._latest_decisions(session, run.run_id)

        views: list[policy.RouteView] = []
        memories: dict[str, ds.RouteMemory] = {}
        for rid in route_ids:
            hist = historical.get(rid, {})
            breakdown = ds.demand_score(
                metrics[rid], self.cfg.demand_score, historical_pph=hist.get(hour),
                peak_soon=ds.peak_starts_soon(clock, hist, self.cfg.demand_score))
            memory = ds.observe(_memory_from_row(states[rid]), breakdown.total, now, self.cfg)
            memory = self._apply_decisions(memory, decided.get(rid, {}), round_trips[rid])
            memories[rid] = memory
            schedule = schedules[rid]
            views.append(policy.RouteView(
                route_id=rid, interval=schedule.interval_minutes, buses=schedule.buses_assigned,
                round_trip=round_trips[rid], passengers_per_hour=metrics[rid].passengers_per_hour,
                score=memory.ema_score, label=memory.peak_label,
                blocked=policy.blocked_reason(memory, now, memory.ema_score, self.cfg)))

        pending_rows = {r.route_id: r for r in session.scalars(
            select(Recommendation).where(Recommendation.run_id == run.run_id,
                                         Recommendation.status == "pending")
            .order_by(Recommendation.id))}
        pending = {rid: policy.Pending(r.id, rid, r.suggested_interval, r.buses_delta,
                                       r.reallocate_from_route_id, r.created_minute)
                   for rid, r in pending_rows.items()}
        plan = policy.plan_routes(views, sim_cfg.fleet.size, sim_cfg.fleet.capacity, self.cfg, pending, now)

        superseded = list(plan.stale)
        created: list[int] = []
        for proposal in plan.proposals:
            if proposal.supersedes is not None:
                superseded.append(proposal.supersedes)
            rec = Recommendation(
                run_id=run.run_id, route_id=proposal.route_id, created_minute=now,
                current_interval=proposal.current_interval, suggested_interval=proposal.suggested_interval,
                buses_delta=proposal.buses_delta, reallocate_from_route_id=proposal.reallocate_from,
                demand_score=round(proposal.score, 3), peak_label=proposal.label,
                est_wait_saving_minutes=proposal.est_wait_saving, reason=proposal.reason,
                warning=proposal.warning, status="pending")
            session.add(rec)
            session.flush()
            created.append(rec.id)
            log.info("recommend %s: %.1f -> %.1f min, buses %+d%s | %s%s", proposal.route_id,
                     proposal.current_interval, proposal.suggested_interval, proposal.buses_delta,
                     f" from {proposal.reallocate_from}" if proposal.reallocate_from else "",
                     proposal.reason, f" | WARNING {proposal.warning}" if proposal.warning else "")
        for rec_id in superseded:
            row = session.get(Recommendation, rec_id)
            if row is not None and row.status == "pending":
                row.status = "superseded"

        if self.auto_approve:
            for rec_id in created:
                rec = session.get(Recommendation, rec_id)
                try:
                    approve_recommendation(session, rec, operator="auto", decided_minute=now,
                                           auto=True, cfg=self.cfg)
                except RecommendationError as exc:
                    log.warning("auto-approve skipped for %s: %s", rec_id, exc)
                    rec.status = "superseded"
                    continue
                memories[rec.route_id] = _with_cooldown(memories[rec.route_id], now, self.cfg,
                                                        round_trips[rec.route_id])

        for rid in route_ids:
            _memory_to_row(states[rid], memories[rid], run.run_id)
        return EvaluationResult(run.run_id, now, created, superseded)

    # ---- queries ------------------------------------------------------------ #
    def _load_states(self, session: Session, run_id: str, route_ids: list[str]) -> dict[str, RouteSchedulerState]:
        rows = {s.route_id: s for s in session.scalars(select(RouteSchedulerState))}
        for rid in route_ids:
            row = rows.get(rid)
            if row is None:
                row = RouteSchedulerState(route_id=rid)
                session.add(row)
                rows[rid] = row
            if row.run_id != run_id:          # new run: forget the previous run's memory
                _memory_to_row(row, ds.RouteMemory(), run_id)
        return rows

    def _window_metrics(self, session: Session, run_id: str, route_ids: list[str],
                        now: float) -> dict[str, ds.WindowMetrics]:
        window = min(self.cfg.evaluation.window_minutes, now) or 1.0
        start = now - window
        arrivals = dict(session.execute(
            select(PassengerJourney.route_id, func.count())
            .where(PassengerJourney.run_id == run_id, PassengerJourney.arrived_minute > start,
                   PassengerJourney.arrived_minute <= now)
            .group_by(PassengerJourney.route_id)).all())
        visits = {r.route_id: r for r in session.execute(
            select(StopVisit.route_id,
                   func.avg(StopVisit.occupancy * 1.0 / StopVisit.capacity).label("occupancy"),
                   func.coalesce(func.sum(StopVisit.left_behind), 0).label("denied"))
            .where(StopVisit.run_id == run_id, StopVisit.departed_minute > start,
                   StopVisit.departed_minute <= now)
            .group_by(StopVisit.route_id)).all()}
        result = {}
        for rid in route_ids:
            v = visits.get(rid)
            result[rid] = ds.WindowMetrics(
                minutes=window, arrivals=arrivals.get(rid, 0),
                denied=int(v.denied) if v else 0,
                avg_occupancy_ratio=float(v.occupancy) if v and v.occupancy is not None else None)
        return result

    def _historical_demand(self, session: Session) -> dict[str, dict[int, float]]:
        if self._historical is None:
            table: dict[str, dict[int, float]] = {}
            for row in session.scalars(select(HistoricalDemand)):
                table.setdefault(row.route_id, {})[row.hour_of_day] = row.passengers_per_hour
            self._historical = table
        return self._historical

    def _latest_decisions(self, session: Session, run_id: str) -> dict[str, dict[str, tuple[float, float]]]:
        """Per route: the latest approval and decline as (decided_minute, demand_score)."""
        rows = session.execute(
            select(Recommendation.route_id, Recommendation.status, Recommendation.demand_score,
                   func.coalesce(OperatorDecision.decided_minute, Recommendation.created_minute))
            .join(OperatorDecision, OperatorDecision.recommendation_id == Recommendation.id)
            .where(Recommendation.run_id == run_id,
                   Recommendation.status.in_([*APPROVED_STATUSES, "declined"]))
            .order_by(OperatorDecision.id)).all()
        latest: dict[str, dict[str, tuple[float, float]]] = {}
        for route_id, status, score, minute in rows:
            kind = "declined" if status == "declined" else "approved"
            latest.setdefault(route_id, {})[kind] = (minute, score)
        return latest

    def _apply_decisions(self, memory: ds.RouteMemory, decided: dict[str, tuple[float, float]],
                         round_trip: float) -> ds.RouteMemory:
        approved, declined = decided.get("approved"), decided.get("declined")
        if approved:
            memory = replace(memory, cooldown_until_minute=approved[0] + policy.cooldown_minutes(self.cfg, round_trip))
        if declined and (approved is None or declined[0] >= approved[0]):
            memory = replace(memory,
                             suppressed_until_minute=declined[0] + policy.suppression_minutes(self.cfg, round_trip),
                             suppressed_score=declined[1])
        else:
            memory = replace(memory, suppressed_until_minute=None, suppressed_score=None)
        return memory


def _with_cooldown(memory: ds.RouteMemory, now: float, cfg: SchedulerConfig, round_trip: float) -> ds.RouteMemory:
    return replace(memory, cooldown_until_minute=now + policy.cooldown_minutes(cfg, round_trip))


def _memory_from_row(row: RouteSchedulerState) -> ds.RouteMemory:
    return ds.RouteMemory(
        ema_score=row.ema_score, peak_label=row.peak_label or ds.NORMAL,
        cooldown_until_minute=row.cooldown_until_minute, suppressed_until_minute=row.suppressed_until_minute,
        suppressed_score=row.suppressed_score, last_evaluated_minute=row.last_evaluated_minute)


def _memory_to_row(row: RouteSchedulerState, memory: ds.RouteMemory, run_id: str) -> None:
    row.run_id = run_id
    row.ema_score = memory.ema_score
    row.peak_label = memory.peak_label
    row.cooldown_until_minute = memory.cooldown_until_minute
    row.suppressed_until_minute = memory.suppressed_until_minute
    row.suppressed_score = memory.suppressed_score
    row.last_evaluated_minute = memory.last_evaluated_minute
    row.is_active = True
