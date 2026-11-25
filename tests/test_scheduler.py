"""Scheduler tests.

Pure functions (no database): the interval formula example from the proposal (240 pax/h,
40 seats, 0.8 load -> 8 min); clamping to 5-30 min; fleet cap; hysteresis labels don't flicker;
cooldown and decline suppression; reallocation only when the donor stays within its max interval.
Service (SQLite in memory): recommendations rows, auto-approve, decline suppression, new runs.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from transit.common.config import load_scheduler_config, load_simulation_config
from transit.common.models import (
    Base,
    OperatorDecision,
    PassengerJourney,
    Recommendation,
    RouteSchedule,
    RouteSchedulerState,
    SimRun,
    StopVisit,
    VehicleState,
)
from transit.db.seed import seed
from transit.scheduler import demand_score as ds
from transit.scheduler import policy
from transit.scheduler.service import (
    RecommendationError,
    SchedulerService,
    approve_recommendation,
    decline_recommendation,
)


@pytest.fixture
def cfg():
    return load_scheduler_config()


def view(route_id="R1", interval=20.0, buses=3, round_trip=60.0, pph=240.0, score=2.0,
         label=ds.NORMAL, blocked=None):
    return policy.RouteView(route_id, interval, buses, round_trip, pph, score, label, blocked)


# --------------------------------------------------------------------------- #
# demand_score
# --------------------------------------------------------------------------- #
def test_proposal_interval_example(cfg):
    assert policy.target_interval(240, 40, cfg.interval) == pytest.approx(8.0)


def test_interval_is_clamped(cfg):
    assert policy.target_interval(5000, 40, cfg.interval) == 5
    assert policy.target_interval(10, 40, cfg.interval) == 30
    assert policy.target_interval(0, 40, cfg.interval) == 30


def test_score_combines_weights_and_offsets(cfg):
    m = ds.WindowMetrics(minutes=15, arrivals=45, denied=15, avg_occupancy_ratio=0.5)
    b = ds.demand_score(m, cfg.demand_score, historical_pph=90, peak_soon=True)
    assert b.arrivals == pytest.approx(3.0)
    assert b.occupancy == pytest.approx(1.0)
    assert b.denied == pytest.approx(3.0)
    assert b.time_of_day == pytest.approx(0.5)
    assert b.popularity == pytest.approx(180 / 90 - 1)
    assert b.total == pytest.approx(8.5)


def test_ema_smooths_towards_new_value():
    assert ds.ema(None, 4.0, 0.4) == 4.0
    assert ds.ema(2.0, 4.0, 0.4) == pytest.approx(2.8)


def test_hysteresis_labels_do_not_flicker(cfg):
    label, labels = ds.NORMAL, []
    for score in [2.0, 3.1, 2.9, 3.05, 2.5, 1.6, 1.4, 1.6, 2.9, 3.0]:
        label = ds.hysteresis_label(label, score, cfg.peak_detection)
        labels.append(label)
    assert labels == ["normal", "peak", "peak", "peak", "peak", "peak",
                      "off_peak", "off_peak", "off_peak", "peak"]


def test_known_peaks_come_from_historical_demand(cfg):
    sim = load_simulation_config("baseline", use_env=False)
    from transit.db.seed import expected_passengers_per_hour
    hist = {h: expected_passengers_per_hour(sim, "R1", h) for h in range(24)}
    assert ds.peak_start_hours(hist, cfg.demand_score.offsets.peak_factor) == [7, 16]
    assert ds.peak_starts_soon(6 * 60 + 45, hist, cfg.demand_score)       # 06:45, peak at 07:00
    assert not ds.peak_starts_soon(6 * 60 + 30, hist, cfg.demand_score)   # 30 min ahead > 20


# --------------------------------------------------------------------------- #
# policy
# --------------------------------------------------------------------------- #
def test_more_buses_from_reserve(cfg):
    r1 = view(pph=240, label=ds.PEAK)  # 8 min on a 60 min loop -> 8 buses
    p = policy.plan_route(r1, [], {"R1": 3}, fleet_size=12, capacity=40, cfg=cfg)
    assert p.suggested_buses == 8 and p.suggested_interval == 8.0
    assert p.buses_delta == 5 and p.reallocate_from is None and p.warning is None
    assert p.est_wait_saving == pytest.approx(6.0)


def test_fleet_cap_lengthens_interval_and_warns(cfg):
    r1 = view(pph=480, label=ds.PEAK)  # wants 5 min -> 12 buses, only 2 free
    p = policy.plan_route(r1, [], {"R1": 3, "R2": 7}, fleet_size=12, capacity=40, cfg=cfg)
    assert p.suggested_buses == 5
    assert p.suggested_interval == 12.0
    assert "no buses available" in p.warning


def test_reserve_target_is_kept_outside_peak(cfg):
    r1 = view(pph=240, label=ds.NORMAL)
    p = policy.plan_route(r1, [], {"R1": 3, "R2": 8}, fleet_size=12, capacity=40, cfg=cfg)
    assert p is None  # the only free bus is the reserve target, and 3 buses already give 20 min
    p = policy.plan_route(view(pph=240, label=ds.PEAK), [], {"R1": 3, "R2": 8}, 12, 40, cfg)
    assert p.suggested_buses == 4


def test_reallocates_from_quietest_route_within_max_interval(cfg):
    r1 = view("R1", pph=240, score=4, label=ds.PEAK)
    quiet = view("R3", interval=20, buses=4, round_trip=60, pph=30, score=0.5, label=ds.OFF_PEAK)
    busy = view("R2", interval=20, buses=5, round_trip=60, pph=200, score=2.5, label=ds.NORMAL)
    assigned = {"R1": 3, "R2": 5, "R3": 4}   # 12 of 12 used, reserve empty
    p = policy.plan_route(r1, [busy, quiet], assigned, 12, 40, cfg)
    # R3 can drop to 2 buses (60/2 = 30 min = max); 3 -> 1 would be 60 min, too long
    assert p.reallocate_from == "R3"
    assert p.suggested_buses == 5 and p.donor_buses_after == 2 and p.donor_interval_after == 30
    assert "reallocating 2" in p.warning


def test_no_reallocation_when_donor_would_exceed_max_interval(cfg):
    r1 = view("R1", interval=30, pph=240, label=ds.PEAK)   # 3 buses could already run 20 min
    donor = view("R3", interval=30, buses=2, round_trip=60, score=0.1, label=ds.OFF_PEAK)
    p = policy.plan_route(r1, [donor], {"R1": 3, "R2": 7, "R3": 2}, 12, 40, cfg)
    assert p.reallocate_from is None and "no buses available" in p.warning
    assert p.suggested_interval == 20


def test_donor_keeps_enough_buses_for_its_own_demand(cfg):
    r3 = view("R3", interval=30, pph=96, score=2.06, label=ds.NORMAL)   # wants 20 min
    r2 = view("R2", interval=20, buses=3, round_trip=60, pph=132, score=2.12, label=ds.NORMAL)
    p = policy.plan_route(r3, [r2], {"R1": 7, "R2": 3, "R3": 2}, 12, 40, cfg)
    assert p is None      # R2 is busier and needs all 3 of its buses, so R3 can't improve

    quiet = view("R2", interval=20, buses=4, round_trip=60, pph=48, score=0.4, label=ds.OFF_PEAK)
    p = policy.plan_route(r3, [quiet], {"R1": 6, "R2": 4, "R3": 2}, 12, 40, cfg)
    assert p.reallocate_from == "R2" and p.donor_buses_after == 3   # needs 2, spares 1 of its 2 extra


def test_peak_routes_are_never_donors(cfg):
    r1 = view("R1", interval=30, pph=240, score=4, label=ds.PEAK)
    other = view("R2", buses=9, round_trip=60, score=3.5, label=ds.PEAK)
    p = policy.plan_route(r1, [other], {"R1": 3, "R2": 9}, 12, 40, cfg)
    assert p.reallocate_from is None and p.suggested_buses == 3


def test_low_demand_releases_buses(cfg):
    p = policy.plan_route(view(pph=40, buses=6, interval=10), [], {"R1": 6}, 12, 40, cfg)
    assert p.suggested_interval == 30 and p.suggested_buses == 2 and p.buses_delta == -4
    assert p.est_wait_saving == pytest.approx(-10.0)


def test_small_changes_are_ignored(cfg):
    # 2.4 seats/min... 60*32/100 = 19.2 min vs 20 now: below min_change_minutes
    assert policy.plan_route(view(pph=100), [], {"R1": 3}, 12, 40, cfg) is None


def test_cooldown_and_decline_suppression(cfg):
    m = ds.RouteMemory(cooldown_until_minute=100)
    assert policy.blocked_reason(m, 90, 3.0, cfg) == "cooldown"
    assert policy.blocked_reason(m, 100, 3.0, cfg) is None

    m = ds.RouteMemory(suppressed_until_minute=100, suppressed_score=2.0)
    assert policy.blocked_reason(m, 50, 2.4, cfg) == "declined"
    assert policy.blocked_reason(m, 50, 2.5, cfg) is None      # escalated by 1.25x
    assert policy.blocked_reason(m, 100, 2.0, cfg) is None     # suppression over

    assert policy.cooldown_minutes(cfg, 57.0) == 57.0


def test_plan_routes_does_not_double_book_the_reserve(cfg):
    a = view("R1", pph=240, score=4, label=ds.PEAK)
    b = view("R2", pph=240, score=3.5, label=ds.PEAK)
    c = view("R3", pph=60, score=0.5, label=ds.OFF_PEAK, interval=30, buses=2)
    plan = policy.plan_routes([a, b, c], 12, 40, cfg)       # 3+3+2 assigned, 4 free
    by_route = {p.route_id: p for p in plan.proposals}
    extra = sum(max(p.buses_delta, 0) for p in plan.proposals if p.reallocate_from is None)
    assert extra <= 4
    assert by_route["R1"].buses_delta == 4                  # busiest route served first


def test_plan_routes_counts_pending_claims_and_supersedes(cfg):
    a = view("R1", pph=240, score=4, label=ds.PEAK)
    b = view("R2", pph=240, score=3.5, label=ds.PEAK)
    pending = {"R1": policy.Pending(11, "R1", 8.0, 5)}
    plan = policy.plan_routes([a, b], 12, 40, cfg, pending)
    assert [p.route_id for p in plan.proposals] == ["R2"]   # R1's pending is still right
    assert plan.proposals[0].suggested_buses == 4           # 12 - 8 (R1 incl. pending) = 4 for R2

    calmer = view("R1", pph=120, score=2, label=ds.NORMAL)  # demand halved: 16 min now
    plan = policy.plan_routes([calmer], 12, 40, cfg, {"R1": policy.Pending(11, "R1", 8.0, 5)})
    assert plan.proposals[0].supersedes == 11

    plan = policy.plan_routes([view("R1", pph=100)], 12, 40, cfg, {"R1": policy.Pending(11, "R1", 8.0, 5)})
    assert plan.proposals == [] and plan.stale == [11]


def test_blocked_routes_are_skipped(cfg):
    plan = policy.plan_routes([view(pph=240, label=ds.PEAK, blocked="cooldown")], 12, 40, cfg)
    assert plan.proposals == []


# --------------------------------------------------------------------------- #
# service (SQLite)
# --------------------------------------------------------------------------- #
RUN = "run-test"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    sim = load_simulation_config("baseline", use_env=False)
    with factory() as session:
        seed(sim, session)
        session.add(SimRun(run_id=RUN, scenario="baseline", seed=42, config=sim.model_dump(mode="json")))
        session.commit()
    return factory


def _traffic(session: Session, route_id: str, now: float, arrivals: int, occupancy: int = 30,
             left_behind: int = 0, run_id: str = RUN):
    import uuid
    for i in range(arrivals):
        session.add(PassengerJourney(
            passenger_id=f"{run_id}-{route_id}-{now}-{i}", run_id=run_id, route_id=route_id,
            origin_stop_id=f"{route_id}-S1", destination_stop_id=f"{route_id}-S2",
            arrived_minute=now - 14 + (i % 14)))
    session.add(StopVisit(event_id=uuid.uuid4(), run_id=run_id, route_id=route_id,
                          stop_id=f"{route_id}-S1", vehicle_id="B01", trip_id="t", departed_minute=now - 1,
                          occupancy=occupancy, capacity=40, boarded=0, alighted=0, left_behind=left_behind))
    vs = session.get(VehicleState, "B01")
    vs.run_id, vs.last_sim_minute = run_id, now


def _busy_r1(session: Session, now: float, **kwargs):
    """R1 crowded (240 pax/h); R2 and R3 at the demand their 20 min interval suits (96 pax/h)."""
    _traffic(session, "R1", now, arrivals=60, **kwargs)
    for rid in ("R2", "R3"):
        _traffic(session, rid, now, arrivals=24, occupancy=20)


def test_service_creates_recommendation_and_state(db, cfg):
    with db() as s:
        _busy_r1(s, 120, left_behind=30)  # 240 pax/h, crowded
        s.commit()
    result = SchedulerService(cfg, db, auto_approve=False).tick()
    assert result.sim_minute == 120 and len(result.created) == 1
    with db() as s:
        rec = s.scalars(select(Recommendation)).one()
        assert rec.route_id == "R1" and rec.status == "pending"
        assert rec.suggested_interval < rec.current_interval and rec.buses_delta > 0
        state = s.get(RouteSchedulerState, "R1")
        assert state.run_id == RUN and state.ema_score > 0 and state.last_evaluated_minute == 120
        assert s.get(RouteSchedule, "R1").version == 1        # not applied without approval


def test_service_waits_for_the_next_evaluation(db, cfg):
    service = SchedulerService(cfg, db, auto_approve=False)
    with db() as s:
        _busy_r1(s, 120)
        s.commit()
    assert service.tick() is not None
    with db() as s:
        s.get(VehicleState, "B01").last_sim_minute = 123
        s.commit()
    assert service.tick() is None                            # < every_minutes later


def test_auto_approve_updates_route_schedules(db, cfg):
    with db() as s:
        _busy_r1(s, 120, left_behind=30)
        s.commit()
    SchedulerService(cfg, db, auto_approve=True).tick()
    with db() as s:
        rec = s.scalars(select(Recommendation)).one()
        sched = s.get(RouteSchedule, "R1")
        assert rec.status == "auto_approved"
        assert sched.version == 2 and sched.source == "auto" and sched.recommendation_id == rec.id
        assert sched.interval_minutes == rec.suggested_interval
        assert sched.buses_assigned == 3 + rec.buses_delta
        decision = s.scalars(select(OperatorDecision)).one()
        assert decision.operator == "auto" and decision.decided_minute == 120
        assert s.get(RouteSchedulerState, "R1").cooldown_until_minute > 120


def test_declined_recommendation_is_suppressed(db, cfg):
    service = SchedulerService(cfg, db, auto_approve=False)
    with db() as s:
        _busy_r1(s, 120)
        s.commit()
    service.tick()
    with db() as s:
        rec = s.scalars(select(Recommendation)).one()
        decline_recommendation(s, rec, operator="operator", decided_minute=121)
        s.commit()
    with db() as s:
        _busy_r1(s, 135)
        s.commit()
    assert service.tick().created == []
    with db() as s:
        state = s.get(RouteSchedulerState, "R1")
        assert state.suppressed_until_minute > 135 and state.suppressed_score is not None


def test_approve_rejects_non_pending_and_oversubscription(db, cfg):
    with db() as s:
        rec = Recommendation(run_id=RUN, route_id="R1", created_minute=1, current_interval=20,
                             suggested_interval=5, buses_delta=10, demand_score=5, peak_label="peak",
                             est_wait_saving_minutes=7.5, reason="test")
        s.add(rec)
        s.flush()
        with pytest.raises(RecommendationError, match="reserve"):
            approve_recommendation(s, rec, operator="operator", decided_minute=1, cfg=cfg)
        rec.status = "declined"
        with pytest.raises(RecommendationError, match="not pending"):
            approve_recommendation(s, rec, operator="operator", decided_minute=1, cfg=cfg)


def test_approve_with_reallocation_updates_donor(db, cfg):
    with db() as s:
        rec = Recommendation(run_id=RUN, route_id="R1", created_minute=1, current_interval=20,
                             suggested_interval=15, buses_delta=1, reallocate_from_route_id="R3",
                             demand_score=5, peak_label="peak", est_wait_saving_minutes=2.5, reason="test")
        s.add(rec)
        s.flush()
        approve_recommendation(s, rec, operator="operator", decided_minute=1, cfg=cfg)
        r1, r3 = s.get(RouteSchedule, "R1"), s.get(RouteSchedule, "R3")
        assert (r1.buses_assigned, r1.version, r1.source) == (4, 2, "operator")
        assert r3.buses_assigned == 2 and r3.version == 2 and r3.interval_minutes == 27.8  # 55.7 min loop / 2
        assert rec.status == "approved"


def test_new_run_resets_scheduler_memory(db, cfg):
    service = SchedulerService(cfg, db, auto_approve=False)
    with db() as s:
        _busy_r1(s, 120)
        s.commit()
    service.tick()
    with db() as s:
        from datetime import datetime, timedelta, timezone
        run = s.get(SimRun, RUN)
        s.add(SimRun(run_id="run-2", scenario="baseline", seed=42, config=run.config,
                     started_at=datetime.now(timezone.utc) + timedelta(minutes=1)))
        _traffic(s, "R2", 10, arrivals=2, run_id="run-2")
        s.commit()
    result = service.tick()
    assert result.run_id == "run-2" and result.sim_minute == 10
    with db() as s:
        state = s.get(RouteSchedulerState, "R1")
        assert state.run_id == "run-2" and state.cooldown_until_minute is None


def test_newer_approval_supersedes_unapplied_older_one(db, cfg):
    def rec(minute, interval):
        return Recommendation(run_id=RUN, route_id="R1", created_minute=minute, current_interval=20,
                              suggested_interval=interval, buses_delta=0, demand_score=4,
                              peak_label="peak", est_wait_saving_minutes=1, reason="test")
    with db() as s:
        first, second = rec(10, 18.6), rec(12, 30)
        s.add_all([first, second])
        s.flush()
        approve_recommendation(s, first, operator="auto", decided_minute=10, auto=True, cfg=cfg)
        approve_recommendation(s, second, operator="auto", decided_minute=12, auto=True, cfg=cfg)
        assert (first.status, second.status) == ("superseded", "auto_approved")
        assert s.get(RouteSchedule, "R1").version == 3


def test_fresh_pending_suggestion_is_left_alone(cfg):
    calmer = view("R1", pph=120, score=2, label=ds.NORMAL)   # would now suggest 16 min, not 8
    fresh = {"R1": policy.Pending(11, "R1", 8.0, 5, created_minute=100)}
    plan = policy.plan_routes([calmer], 12, 40, cfg, fresh, now=110)
    assert plan.proposals == [] and plan.stale == []          # operator still has time to decide
    plan = policy.plan_routes([calmer], 12, 40, cfg, fresh, now=130)
    assert plan.proposals[0].supersedes == 11                 # 30 sim minutes later it's refreshed


def test_pending_release_does_not_free_a_bus_yet(cfg):
    busy = view("R1", pph=480, score=4, label=ds.PEAK)                 # wants 12 buses
    quiet = view("R3", pph=30, score=0.5, label=ds.OFF_PEAK, blocked="cooldown")
    pending = {"R3": policy.Pending(5, "R3", 30.0, -1)}                # R3 offers a bus, not approved
    plan = policy.plan_routes([busy, quiet], 12, 40, cfg, pending)     # assigned 3+3, 6 free
    assert plan.proposals[0].suggested_buses == 3 + 6                  # not 3 + 7
