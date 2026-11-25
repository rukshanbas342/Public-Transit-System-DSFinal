"""Read-side queries for the active run (latest sim_runs row). Routers call these."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from transit.api import schemas
from transit.api.broker import BrokerProbe
from transit.common.config import SimulationConfig, format_clock
from transit.common.models import (
    OperatorDecision,
    PassengerJourney,
    ProcessedEvent,
    Recommendation,
    Route,
    RouteSchedule,
    RouteSchedulerState,
    SimRun,
    Stop,
    StopVisit,
    Vehicle,
    VehicleState,
)
from transit.db.seed import simulator_connected
from transit.scheduler.service import active_run, current_sim_minute

RECENT_WAIT_MINUTES = 60


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _age(dt: datetime | None, now: datetime) -> float | None:
    dt = _aware(dt)
    return None if dt is None else max(0.0, (now - dt).total_seconds())


def wait_stats(waits: Iterable[float]) -> tuple[float | None, float | None]:
    """Average and p95 (nearest rank), the same definition as the simulator's summary."""
    w = sorted(waits)
    if not w:
        return None, None
    p95 = w[min(len(w) - 1, math.ceil(0.95 * len(w)) - 1)]
    return round(sum(w) / len(w), 2), round(p95, 2)


class RunContext:
    """The active run plus its config and clock, loaded once per request."""

    def __init__(self, session: Session):
        self.session = session
        self.run: SimRun | None = active_run(session)
        self.cfg: SimulationConfig | None = None
        self.sim_minute: float | None = None
        if self.run is not None:
            try:
                self.cfg = SimulationConfig.model_validate(self.run.config)
            except ValueError:
                self.cfg = None
            self.sim_minute = current_sim_minute(session, self.run.run_id)

    @property
    def run_id(self) -> str | None:
        return self.run.run_id if self.run else None

    def clock(self, minute: float | None) -> str | None:
        if minute is None or self.cfg is None:
            return None
        return format_clock(self.cfg.time.start_minute + minute)

    def info(self) -> schemas.RunInfo | None:
        if self.run is None:
            return None
        cfg = self.cfg
        return schemas.RunInfo(
            run_id=self.run.run_id, scenario=self.run.scenario, seed=self.run.seed,
            service_start=cfg.time.service_start if cfg else "", service_end=cfg.time.service_end if cfg else "",
            started_at=_aware(self.run.started_at), finished_at=_aware(self.run.finished_at),
            sim_minute=self.sim_minute, clock=self.clock(self.sim_minute),
            duration_minutes=cfg.time.duration_minutes if cfg else 0, summary=self.run.summary)


# --------------------------------------------------------------------------- #
# Network
# --------------------------------------------------------------------------- #
def _journeys(ctx: RunContext):
    if ctx.run_id is None:
        return []
    return ctx.session.execute(
        select(PassengerJourney.route_id, PassengerJourney.origin_stop_id, PassengerJourney.arrived_minute,
               PassengerJourney.boarded_minute, PassengerJourney.times_denied)
        .where(PassengerJourney.run_id == ctx.run_id)).all()


def routes_and_totals(ctx: RunContext, journeys=None) -> tuple[list[schemas.RouteStatus], schemas.Totals | None]:
    s = ctx.session
    journeys = _journeys(ctx) if journeys is None else journeys
    routes = s.scalars(select(Route).order_by(Route.id)).all()
    schedules = {r.route_id: r for r in s.scalars(select(RouteSchedule))}
    states = {r.route_id: r for r in s.scalars(select(RouteSchedulerState))}
    on_route = dict(s.execute(select(VehicleState.route_id, func.count())
                              .where(VehicleState.route_id.is_not(None)).group_by(VehicleState.route_id)).all())
    stop_counts = dict(s.execute(select(Stop.route_id, func.count()).group_by(Stop.route_id)).all())
    occupancy = {}
    if ctx.run_id:
        occupancy = dict(s.execute(
            select(StopVisit.route_id, func.avg(StopVisit.occupancy * 1.0 / StopVisit.capacity))
            .where(StopVisit.run_id == ctx.run_id).group_by(StopVisit.route_id)).all())

    per_route: dict[str, dict] = defaultdict(lambda: {"arrived": 0, "served": 0, "waiting": 0, "left": 0,
                                                      "waits": [], "recent": []})
    recent_from = (ctx.sim_minute or 0) - RECENT_WAIT_MINUTES
    for route_id, _stop, arrived, boarded, denied in journeys:
        r = per_route[route_id]
        r["arrived"] += 1
        if denied:
            r["left"] += 1
        if boarded is None:
            r["waiting"] += 1
        else:
            r["served"] += 1
            r["waits"].append(boarded - arrived)
            if boarded >= recent_from:
                r["recent"].append(boarded - arrived)

    dwell = ctx.cfg.fleet.dwell.base_seconds / 60 if ctx.cfg else 20 / 60
    result = []
    for route in routes:
        r, sched, state = per_route[route.id], schedules.get(route.id), states.get(route.id)
        same_run = state is not None and state.run_id == ctx.run_id
        avg, p95 = wait_stats(r["waits"])
        recent_avg, _ = wait_stats(r["recent"])
        result.append(schemas.RouteStatus(
            route_id=route.id, name=route.name,
            round_trip_minutes=round(route.travel_minutes + stop_counts.get(route.id, 0) * dwell, 1),
            interval_minutes=sched.interval_minutes if sched else 0,
            buses_assigned=sched.buses_assigned if sched else 0,
            buses_on_route=on_route.get(route.id, 0),
            schedule_version=sched.version if sched else 0,
            applied_version=sched.applied_version if sched else None,
            schedule_source=sched.source if sched else "",
            peak_label=state.peak_label if same_run else "normal",
            demand_score=round(state.ema_score, 2) if same_run and state.ema_score is not None else None,
            cooldown_until_minute=state.cooldown_until_minute if same_run else None,
            arrived=r["arrived"], served=r["served"], waiting=r["waiting"], left_behind=r["left"],
            avg_wait=avg, p95_wait=p95, recent_avg_wait=recent_avg,
            avg_occupancy_ratio=round(float(occupancy[route.id]), 3) if occupancy.get(route.id) is not None else None,
        ))

    totals = None
    if ctx.run_id:
        all_waits = [w for r in per_route.values() for w in r["waits"]]
        avg, p95 = wait_stats(all_waits)
        totals = schemas.Totals(
            arrived=sum(r["arrived"] for r in per_route.values()),
            served=sum(r["served"] for r in per_route.values()),
            waiting=sum(r["waiting"] for r in per_route.values()),
            left_behind=sum(r["left"] for r in per_route.values()),
            avg_wait=avg, p95_wait=p95)
    return result, totals


def vehicles(ctx: RunContext, now: datetime) -> list[schemas.VehicleStatus]:
    rows = ctx.session.execute(
        select(VehicleState, Vehicle.capacity).join(Vehicle, Vehicle.id == VehicleState.vehicle_id)
        .order_by(VehicleState.vehicle_id)).all()
    result = []
    for vs, capacity in rows:
        current = vs.run_id is not None and vs.run_id == ctx.run_id
        result.append(schemas.VehicleStatus(
            vehicle_id=vs.vehicle_id, route_id=vs.route_id, status=vs.status if current else "idle",
            stop_id=vs.stop_id if current else None, stop_index=vs.stop_index if current else None,
            occupancy=vs.occupancy if current else 0, capacity=capacity,
            last_sim_minute=vs.last_sim_minute if current else None,
            updated_at=_aware(vs.updated_at), age_seconds=_age(vs.updated_at, now) if current else None))
    return result


def stops(ctx: RunContext, journeys=None) -> list[schemas.StopStatus]:
    journeys = _journeys(ctx) if journeys is None else journeys
    waiting: dict[str, int] = defaultdict(int)
    for _route, stop_id, _arrived, boarded, _denied in journeys:
        if boarded is None:
            waiting[stop_id] += 1
    return [schemas.StopStatus(stop_id=st.id, route_id=st.route_id, stop_index=st.stop_index, name=st.name,
                               waiting=waiting.get(st.id, 0))
            for st in ctx.session.scalars(select(Stop).order_by(Stop.route_id, Stop.stop_index))]


# --------------------------------------------------------------------------- #
# Recommendations
# --------------------------------------------------------------------------- #
def to_schema(ctx: RunContext, rec: Recommendation, decision: OperatorDecision | None) -> schemas.Recommendation:
    return schemas.Recommendation(
        id=rec.id, route_id=rec.route_id, created_minute=rec.created_minute,
        created_clock=ctx.clock(rec.created_minute), current_interval=rec.current_interval,
        suggested_interval=rec.suggested_interval, buses_delta=rec.buses_delta,
        reallocate_from_route_id=rec.reallocate_from_route_id, demand_score=rec.demand_score,
        peak_label=rec.peak_label, est_wait_saving_minutes=rec.est_wait_saving_minutes, reason=rec.reason,
        warning=rec.warning, status=rec.status,
        decided_by=decision.operator if decision else None,
        decided_minute=decision.decided_minute if decision else None)


def recommendations(ctx: RunContext, statuses: list[str] | None = None, exclude: list[str] | None = None,
                    limit: int = 50) -> list[schemas.Recommendation]:
    if ctx.run_id is None:
        return []
    query = (select(Recommendation, OperatorDecision)
             .outerjoin(OperatorDecision, OperatorDecision.recommendation_id == Recommendation.id)
             .where(Recommendation.run_id == ctx.run_id))
    if statuses:
        query = query.where(Recommendation.status.in_(statuses))
    if exclude:
        query = query.where(Recommendation.status.not_in(exclude))
    rows = ctx.session.execute(query.order_by(Recommendation.id.desc()).limit(limit)).all()
    return [to_schema(ctx, rec, decision) for rec, decision in rows]


# --------------------------------------------------------------------------- #
# System health
# --------------------------------------------------------------------------- #
def system_health(ctx: RunContext, probe: BrokerProbe | None, now: datetime) -> schemas.SystemHealth:
    s = ctx.session
    broker = probe.stats() if probe else None
    events_processed, per_second, last_age = 0, 0.0, None
    if ctx.run_id:
        events_processed = s.scalar(select(func.count()).select_from(ProcessedEvent)
                                    .where(ProcessedEvent.run_id == ctx.run_id)) or 0
        last = s.scalar(select(func.max(ProcessedEvent.processed_at)).where(ProcessedEvent.run_id == ctx.run_id))
        last_age = _age(last, now)
        if last_age is not None and last_age < 10:
            # Count against the newest event's own clock, so DB and API clocks needn't agree.
            since = _aware(last) - timedelta(seconds=10)
            recent = s.scalar(select(func.count()).select_from(ProcessedEvent)
                              .where(ProcessedEvent.run_id == ctx.run_id, ProcessedEvent.processed_at > since))
            per_second = round((recent or 0) / 10, 1)
    states = s.scalars(select(RouteSchedulerState).where(RouteSchedulerState.run_id == ctx.run_id)).all() \
        if ctx.run_id else []
    evaluated = [st.last_evaluated_minute for st in states if st.last_evaluated_minute is not None]
    updated = [_aware(st.updated_at) for st in states if st.updated_at is not None]
    auto = bool(ctx.run_id and s.scalar(
        select(func.count()).select_from(OperatorDecision)
        .join(Recommendation, Recommendation.id == OperatorDecision.recommendation_id)
        .where(Recommendation.run_id == ctx.run_id, OperatorDecision.operator == "auto")))
    is_pg = s.get_bind().dialect.name == "postgresql"

    def queue(q):
        return schemas.QueueStatus(name=q.name, messages=q.messages, consumers=q.consumers) if q else None

    return schemas.SystemHealth(
        database_ok=True,
        broker_ok=bool(broker and broker.ok), broker_error=broker.error if broker else "not checked",
        events_queue=queue(broker.events) if broker else None,
        dead_letter_queue=queue(broker.dead) if broker else None,
        workers=broker.events.consumers if broker and broker.events else None,
        simulator_connected=simulator_connected(s) if is_pg else None,
        events_processed=events_processed, events_per_second=per_second, last_event_age_seconds=last_age,
        scheduler_last_evaluated_minute=max(evaluated) if evaluated else None,
        scheduler_age_seconds=_age(max(updated), now) if updated else None,
        auto_approve_hint=auto)


def overview(session: Session, probe: BrokerProbe | None) -> schemas.Overview:
    now = utcnow()
    ctx = RunContext(session)
    journeys = _journeys(ctx)
    routes, totals = routes_and_totals(ctx, journeys)
    return schemas.Overview(
        server_time=now, run=ctx.info(), totals=totals, routes=routes, vehicles=vehicles(ctx, now),
        stops=stops(ctx, journeys), pending=recommendations(ctx, ["pending"]),
        history=recommendations(ctx, exclude=["pending"], limit=30),
        system=system_health(ctx, probe, now))

