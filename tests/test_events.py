import pytest
from pydantic import ValidationError

from transit.common import events as ev
from transit.common.config import load_simulation_config
from transit.simulator.engine import TransitSimulation
from transit.simulator.sinks import MemorySink


@pytest.fixture(scope="module")
def sample_events():
    cfg = load_simulation_config("breakdown", ["time.service_start=07:00", "time.service_end=09:00",
                                               "time.realtime_seconds_per_sim_minute=0"], use_env=False)
    sink = MemorySink()
    TransitSimulation(cfg, sink).run()
    return sink.events


def test_every_event_round_trips_through_json(sample_events):
    for event in sample_events:
        parsed = ev.parse_event(ev.serialize(event))
        assert type(parsed) is type(event)
        assert parsed == event


def test_routing_key_is_event_type(sample_events):
    assert all(e.routing_key == e.event_type for e in sample_events)


def test_contract_lists_all_types():
    assert "passenger.denied" in ev.EVENT_TYPES and "schedule.applied" in ev.EVENT_TYPES
    assert len(ev.EVENT_TYPES) == 15


def test_unknown_event_type_is_rejected():
    with pytest.raises(ValidationError):
        ev.parse_event({"event_type": "vehicle.teleported", "run_id": "x", "sim_minute": 0, "clock": "05:00"})
