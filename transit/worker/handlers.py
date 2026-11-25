"""Database handlers for events consumed from RabbitMQ."""

from __future__ import annotations

import logging
from typing import Callable

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from transit.common import events as ev
from transit.common.models import (
    PassengerJourney,
    ProcessedEvent,
    Recommendation,
    RouteSchedule,
    SimRun,
    StopVisit,
    VehicleState,
)

log = logging.getLogger(__name__)


def _vehicle_state(session: Session, event: ev.VehicleEvent) -> VehicleState | None:
    """Lock a bus row and return it when this event is newer than its state."""
    state = session.execute(
        select(VehicleState)
        .where(VehicleState.vehicle_id == event.bus_id)
        .with_for_update()
    ).scalar_one_or_none()
    if state is None:
        raise LookupError(f"vehicle state does not exist for {event.bus_id}")
    if state.run_id == event.run_id and event.seq <= state.last_seq:
        return None
    state.run_id = event.run_id
    state.last_seq = event.seq
    state.last_sim_minute = event.sim_minute
    return state


def _handle_run_started(session: Session, event: ev.RunStarted) -> None:
    session.add(SimRun(
        run_id=event.run_id,
        scenario=event.scenario,
        seed=event.seed,
        config=event.config,
    ))


def _handle_run_finished(session: Session, event: ev.RunFinished) -> None:
    run = session.get(SimRun, event.run_id)
    if run is None:
        raise LookupError(f"run does not exist for {event.run_id}")
    run.summary = event.summary
    run.finished_at = event.emitted_at


def _handle_bus_assigned(session: Session, event: ev.BusAssigned) -> None:
    state = _vehicle_state(session, event)
    if state is None:
        return
    state.route_id = event.to_route_id
    state.trip_id = None
    state.stop_id = None
    state.stop_index = None
    state.status = "idle"
    state.occupancy = 0


def _handle_bus_departed_terminal(session: Session, event: ev.BusDepartedTerminal) -> None:
    state = _vehicle_state(session, event)
    if state is None:
        return
    state.route_id = event.route_id
    state.trip_id = event.trip_id
    state.stop_id = None
    state.stop_index = None
    state.status = "in_service"
    state.occupancy = event.occupancy


def _handle_bus_arrived_stop(session: Session, event: ev.BusArrivedStop) -> None:
    state = _vehicle_state(session, event)
    if state is None:
        return
    state.route_id = event.route_id
    state.trip_id = event.trip_id
    state.stop_id = event.stop_id
    state.stop_index = event.stop_index
    state.status = "at_stop"
    state.occupancy = event.occupancy


def _handle_bus_departed_stop(session: Session, event: ev.BusDepartedStop) -> None:
    state = _vehicle_state(session, event)
    if state is None:
        return
    state.route_id = event.route_id
    state.trip_id = event.trip_id
    state.stop_id = event.stop_id
    state.stop_index = event.stop_index
    state.status = "in_service"
    state.occupancy = event.occupancy
    session.add(StopVisit(
        event_id=event.event_id,
        run_id=event.run_id,
        route_id=event.route_id,
        stop_id=event.stop_id,
        vehicle_id=event.bus_id,
        trip_id=event.trip_id,
        departed_minute=event.sim_minute,
        occupancy=event.occupancy,
        capacity=event.capacity,
        boarded=event.boarded,
        alighted=event.alighted,
        left_behind=event.left_behind,
    ))


def _handle_bus_completed_trip(session: Session, event: ev.BusCompletedTrip) -> None:
    state = _vehicle_state(session, event)
    if state is None:
        return
    state.route_id = event.route_id
    state.trip_id = None
    state.status = "idle"
    state.occupancy = 0


def _handle_bus_delayed(session: Session, event: ev.BusDelayed) -> None:
    state = _vehicle_state(session, event)
    if state is not None:
        state.route_id = event.route_id
        state.trip_id = event.trip_id


def _handle_bus_broken_down(session: Session, event: ev.BusBrokeDown) -> None:
    state = _vehicle_state(session, event)
    if state is None:
        return
    state.route_id = event.route_id
    state.trip_id = event.trip_id
    state.stop_id = event.after_stop_id
    state.status = "broken_down"


def _handle_bus_recovered(session: Session, event: ev.BusRecovered) -> None:
    state = _vehicle_state(session, event)
    if state is None:
        return
    state.route_id = event.route_id
    state.trip_id = event.trip_id
    state.status = "in_service"


def _handle_passenger_arrived(session: Session, event: ev.PassengerArrived) -> None:
    session.add(PassengerJourney(
        passenger_id=event.passenger_id,
        run_id=event.run_id,
        route_id=event.route_id,
        origin_stop_id=event.stop_id,
        destination_stop_id=event.destination_stop_id,
        arrived_minute=event.sim_minute,
        times_denied=0,
    ))


def _journey(session: Session, passenger_id: str) -> PassengerJourney:
    journey = session.get(PassengerJourney, passenger_id)
    if journey is None:
        raise LookupError(f"passenger journey does not exist for {passenger_id}")
    return journey


def _handle_passenger_boarded(session: Session, event: ev.PassengerBoarded) -> None:
    journey = _journey(session, event.passenger_id)
    journey.boarded_minute = event.sim_minute
    journey.vehicle_id = event.bus_id


def _handle_passenger_denied(session: Session, event: ev.PassengerDenied) -> None:
    journey = _journey(session, event.passenger_id)
    journey.times_denied += 1


def _handle_passenger_alighted(session: Session, event: ev.PassengerAlighted) -> None:
    journey = _journey(session, event.passenger_id)
    journey.alighted_minute = event.sim_minute


def _handle_schedule_applied(session: Session, event: ev.ScheduleApplied) -> None:
    schedule = session.get(RouteSchedule, event.route_id)
    if schedule is None:
        raise LookupError(f"route schedule does not exist for {event.route_id}")
    schedule.applied_version = max(schedule.applied_version or 0, event.version)
    if event.recommendation_id is not None:
        recommendation = session.get(Recommendation, event.recommendation_id)
        if recommendation is not None:
            recommendation.status = "applied"


HANDLERS: dict[str, Callable[[Session, ev.BaseEvent], None]] = {
    "run.started": _handle_run_started,
    "run.finished": _handle_run_finished,
    "vehicle.assigned": _handle_bus_assigned,
    "vehicle.departed_terminal": _handle_bus_departed_terminal,
    "vehicle.arrived_stop": _handle_bus_arrived_stop,
    "vehicle.departed_stop": _handle_bus_departed_stop,
    "vehicle.completed_trip": _handle_bus_completed_trip,
    "vehicle.delayed": _handle_bus_delayed,
    "vehicle.breakdown": _handle_bus_broken_down,
    "vehicle.recovered": _handle_bus_recovered,
    "passenger.arrived": _handle_passenger_arrived,
    "passenger.boarded": _handle_passenger_boarded,
    "passenger.denied": _handle_passenger_denied,
    "passenger.alighted": _handle_passenger_alighted,
    "schedule.applied": _handle_schedule_applied,
}


def _record_processed(session: Session, event: ev.BaseEvent) -> bool:
    values = {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "run_id": event.run_id,
    }
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        result = session.execute(
            pg_insert(ProcessedEvent)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[ProcessedEvent.event_id])
            .returning(ProcessedEvent.event_id)
        )
        return result.scalar_one_or_none() is not None

    existing = session.get(ProcessedEvent, event.event_id)
    if existing is not None:
        return False
    session.add(ProcessedEvent(**values))
    session.flush()
    return True


def process_event(session: Session, event: ev.BaseEvent) -> bool:
    """Apply one event in the caller's transaction.

    Returns ``False`` when the event was already processed. Any exception is
    allowed to escape so the caller rolls back and the broker redelivers it.
    """
    if not _record_processed(session, event):
        return False
    try:
        handler = HANDLERS[event.event_type]
    except KeyError as exc:
        raise ValueError(f"unsupported event type {event.event_type!r}") from exc
    handler(session, event)
    return True
