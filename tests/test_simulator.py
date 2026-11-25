from collections import defaultdict

import pytest

from transit.common.config import load_simulation_config
from transit.simulator.engine import TransitSimulation
from transit.simulator.schedule_source import CallbackScheduleSource, ScheduleEntry, initial_schedule
from transit.simulator.sinks import MemorySink

FAST = ["time.realtime_seconds_per_sim_minute=0"]


def run(scenario="quick_test", overrides=(), source=None):
    cfg = load_simulation_config(scenario, [*FAST, "run.run_id=test", *overrides], use_env=False)
    sink = MemorySink()
    summary = TransitSimulation(cfg, sink, source).run()
    return cfg, sink, summary


def test_same_seed_gives_identical_runs():
    _, a, sa = run()
    _, b, sb = run()
    assert sa["totals"] == sb["totals"]
    assert [e.event_type for e in a.events] == [e.event_type for e in b.events]


def test_different_seed_changes_arrivals():
    _, _, sa = run(overrides=["run.seed=1"])
    _, _, sb = run(overrides=["run.seed=2"])
    assert sa["totals"]["arrived"] != sb["totals"]["arrived"]


def test_passenger_arrivals_do_not_depend_on_schedule():
    """Fixed vs adaptive comparisons are only fair if both see the same passengers."""
    cfg, fixed, _ = run("rush_hour", ["time.service_start=06:30", "time.service_end=09:30"])
    base = initial_schedule(cfg)
    adaptive_source = CallbackScheduleSource(
        lambda t: {**base, "R1": ScheduleEntry("R1", 2, 10, 6)} if t >= 30 else base)
    _, adaptive, _ = run("rush_hour", ["time.service_start=06:30", "time.service_end=09:30"], adaptive_source)

    def arrivals(sink):
        return [(e.passenger_id, e.sim_minute) for e in sink.of_type("passenger.arrived")]

    assert arrivals(fixed) == arrivals(adaptive)


def test_bus_never_exceeds_capacity_and_left_behind_is_reported():
    cfg, sink, summary = run("rush_hour", ["time.service_start=07:00", "time.service_end=09:30"])
    departs = sink.of_type("vehicle.departed_stop")
    assert all(e.occupancy <= cfg.fleet.capacity for e in departs)
    assert summary["totals"]["passengers_left_behind"] > 0   # surge overloads route A
    assert len(sink.of_type("passenger.denied")) == summary["totals"]["left_behind_events"]


def test_vehicle_sequence_numbers_strictly_increase():
    _, sink, _ = run()
    seqs = defaultdict(list)
    for e in sink.events:
        if hasattr(e, "seq"):
            seqs[e.bus_id].append(e.seq)
    for values in seqs.values():
        assert values == list(range(1, len(values) + 1))


def test_passengers_are_conserved():
    _, sink, summary = run()
    t = summary["totals"]
    assert t["arrived"] == t["served"] + t["still_waiting"]
    assert len(sink.of_type("passenger.boarded")) == t["served"]
    on_board_at_end = t["served"] - len(sink.of_type("passenger.alighted"))
    assert on_board_at_end >= 0


def test_new_schedule_is_applied_at_next_departure_and_pulls_reserve_buses():
    cfg = load_simulation_config("quick_test", [*FAST, "run.run_id=test"], use_env=False)
    base = initial_schedule(cfg)
    source = CallbackScheduleSource(
        lambda t: {**base, "R1": ScheduleEntry("R1", 2, 10, 5, recommendation_id=99)} if t >= 25 else base)
    sink = MemorySink()
    TransitSimulation(cfg, sink, source).run()

    start = [e for e in sink.of_type("schedule.applied") if e.sim_minute == 0]
    assert {e.route_id for e in start} == set(base) and all(e.version == 1 for e in start)
    applied = [e for e in sink.of_type("schedule.applied") if e.sim_minute > 0]
    assert len(applied) == 1 and applied[0].route_id == "R1" and applied[0].recommendation_id == 99
    assert applied[0].sim_minute == pytest.approx(40)       # departures at 0, 20, 40 -> applies at 40
    to_r1 = [e for e in sink.of_type("vehicle.assigned") if e.to_route_id == "R1" and e.sim_minute > 0]
    assert len(to_r1) == 2                                   # 3 -> 5 buses, both from reserve
    later = [e for e in sink.of_type("vehicle.departed_terminal") if e.route_id == "R1" and e.sim_minute > 40]
    assert all(e.interval_minutes == 10 for e in later)


def test_reducing_buses_returns_them_to_reserve():
    cfg = load_simulation_config("quick_test", [*FAST], use_env=False)
    base = initial_schedule(cfg)
    source = CallbackScheduleSource(lambda t: {**base, "R3": ScheduleEntry("R3", 2, 30, 2)} if t >= 1 else base)
    sink = MemorySink()
    sim = TransitSimulation(cfg, sink, source)
    sim.run()
    assert len(sim.routes["R3"].buses) == 2
    assert any(e.from_route_id == "R3" and e.to_route_id is None for e in sink.of_type("vehicle.assigned"))


def test_scheduled_breakdown_emits_breakdown_and_recovery():
    _, sink, _ = run("breakdown", ["time.service_start=07:00", "time.service_end=09:30"])
    down = sink.of_type("vehicle.breakdown")
    up = sink.of_type("vehicle.recovered")
    assert [e.bus_id for e in down] == ["B04"] and [e.bus_id for e in up] == ["B04"]
    assert up[0].sim_minute - down[0].sim_minute == pytest.approx(30)


def test_run_events_bracket_the_stream():
    _, sink, summary = run()
    assert sink.events[0].event_type == "run.started"
    assert sink.events[-1].event_type == "run.finished"
    assert sink.events[-1].summary == summary
