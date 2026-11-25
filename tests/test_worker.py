import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from transit.common import events as ev
from transit.common.models import (
    Base,
    PassengerJourney,
    ProcessedEvent,
    Recommendation,
    Route,
    RouteSchedule,
    SimRun,
    Stop,
    StopVisit,
    Vehicle,
    VehicleState,
)
from transit.worker.handlers import process_event


def _event(event_cls, *, run_id="run-1", sim_minute=1, **kwargs):
    return event_cls(
        event_id=uuid.uuid4(),
        run_id=run_id,
        sim_minute=sim_minute,
        clock="05:01",
        **kwargs,
    )


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add_all([
        Route(id="R1", name="Route 1", popularity=1, travel_minutes=20),
        Stop(id="R1-S0", route_id="R1", stop_index=0, name="Terminal", travel_to_next=10, weight=1),
        Stop(id="R1-S1", route_id="R1", stop_index=1, name="Stop 1", travel_to_next=10, weight=1),
        Vehicle(id="B01", capacity=40),
        VehicleState(vehicle_id="B01", status="idle", occupancy=0, last_seq=0),
        RouteSchedule(route_id="R1", version=1, interval_minutes=20, buses_assigned=1, source="seed"),
        Recommendation(
            id=7, run_id="run-1", route_id="R1", created_minute=1,
            current_interval=20, suggested_interval=10, buses_delta=0,
            demand_score=1, peak_label="peak", est_wait_saving_minutes=2,
            reason="test", status="approved",
        ),
    ])
    session.commit()
    return session


def test_duplicate_event_is_applied_once():
    session = _session()
    event = _event(
        ev.BusAssigned,
        bus_id="B01",
        seq=1,
        from_route_id=None,
        to_route_id="R1",
        reason="test",
    )

    assert process_event(session, event) is True
    session.commit()
    assert process_event(session, event) is False
    session.commit()

    state = session.get(VehicleState, "B01")
    assert state.route_id == "R1"
    assert session.scalar(select(ProcessedEvent).where(ProcessedEvent.event_id == event.event_id))


def test_vehicle_sequence_guard_resets_for_a_new_run():
    session = _session()
    first = _event(
        ev.BusAssigned,
        bus_id="B01",
        seq=4,
        from_route_id=None,
        to_route_id="R1",
        reason="first",
    )
    old = _event(
        ev.BusDepartedTerminal,
        bus_id="B01",
        seq=3,
        route_id="R1",
        trip_id="old-trip",
        scheduled_minute=1,
        interval_minutes=20,
        occupancy=2,
    )
    new = _event(
        ev.BusDepartedTerminal,
        run_id="run-2",
        bus_id="B01",
        seq=1,
        route_id="R1",
        trip_id="new-trip",
        scheduled_minute=1,
        interval_minutes=20,
        occupancy=3,
    )

    assert process_event(session, first) is True
    session.commit()
    assert process_event(session, old) is True
    session.commit()
    assert process_event(session, new) is True
    session.commit()

    state = session.get(VehicleState, "B01")
    assert (state.run_id, state.last_seq, state.trip_id, state.occupancy) == (
        "run-2", 1, "new-trip", 3
    )


def test_event_handlers_update_worker_owned_tables():
    session = _session()
    events = [
        _event(
            ev.RunStarted,
            scenario="quick_test",
            seed=42,
            service_start="05:00",
            service_end="23:00",
            config={"test": True},
        ),
        _event(
            ev.PassengerArrived,
            passenger_id="p1",
            route_id="R1",
            stop_id="R1-S0",
            destination_stop_id="R1-S1",
        ),
        _event(
            ev.PassengerBoarded,
            sim_minute=3,
            passenger_id="p1",
            route_id="R1",
            stop_id="R1-S0",
            bus_id="B01",
            trip_id="R1-T0001",
            wait_minutes=2,
        ),
        _event(
            ev.BusArrivedStop,
            sim_minute=4,
            bus_id="B01",
            seq=1,
            route_id="R1",
            trip_id="R1-T0001",
            stop_id="R1-S0",
            stop_index=0,
            occupancy=1,
        ),
        _event(
            ev.BusDepartedStop,
            sim_minute=5,
            bus_id="B01",
            seq=2,
            route_id="R1",
            trip_id="R1-T0001",
            stop_id="R1-S0",
            stop_index=0,
            occupancy=1,
            boarded=1,
            alighted=0,
            left_behind=0,
            capacity=40,
        ),
        _event(
            ev.PassengerAlighted,
            sim_minute=10,
            passenger_id="p1",
            route_id="R1",
            stop_id="R1-S1",
            bus_id="B01",
            trip_id="R1-T0001",
            travel_minutes=5,
        ),
        _event(
            ev.ScheduleApplied,
            sim_minute=11,
            route_id="R1",
            version=2,
            interval_minutes=10,
            buses_assigned=1,
            recommendation_id=7,
        ),
        _event(ev.RunFinished, sim_minute=12, summary={"served": 1}),
    ]

    for event in events:
        assert process_event(session, event) is True
        session.commit()

    assert session.get(PassengerJourney, "p1").alighted_minute == 10
    visit = session.scalar(select(StopVisit))
    assert (visit.boarded, visit.capacity, visit.vehicle_id) == (1, 40, "B01")
    assert session.get(RouteSchedule, "R1").applied_version == 2
    assert session.get(Recommendation, 7).status == "applied"
    assert session.get(SimRun, "run-1").summary == {"served": 1}
