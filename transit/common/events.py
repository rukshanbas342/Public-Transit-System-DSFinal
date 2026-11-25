"""The event contract between the simulator and everything downstream.

Every message on RabbitMQ is one of the models below, serialized as JSON.
Changing a field here is a breaking change for workers: add fields as
optional, never rename or remove without updating every consumer.

Envelope fields (on every event)
    event_id     globally unique; workers use it to ignore redelivered duplicates
    event_type   discriminator, also used as the RabbitMQ routing key
    run_id       which simulation run produced it
    sim_minute   simulated minutes since service start (float)
    clock        time of day "HH:MM" (for humans / dashboards)
    emitted_at   wall-clock UTC time the simulator created the event

Vehicle events also carry `seq`: a counter per bus that only goes up. A worker
must ignore a vehicle event whose seq is <= the stored last_seq for that bus,
so a late, redelivered message can never move a bus backwards.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter

EXCHANGE = "transit.events"          # durable topic exchange
EVENTS_QUEUE = "transit.events.process"   # durable queue the workers consume (competing consumers)
DEAD_LETTER_EXCHANGE = "transit.events.dlx"
DEAD_LETTER_QUEUE = "transit.events.dead"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class BaseEvent(BaseModel):
    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    run_id: str
    sim_minute: float
    clock: str
    emitted_at: datetime = Field(default_factory=_now)

    @property
    def routing_key(self) -> str:
        return self.event_type  # type: ignore[attr-defined]


class VehicleEvent(BaseEvent):
    bus_id: str
    seq: int = Field(ge=1)


# ----- run lifecycle --------------------------------------------------------- #
class RunStarted(BaseEvent):
    event_type: Literal["run.started"] = "run.started"
    scenario: str
    seed: int
    service_start: str
    service_end: str
    config: dict           # full resolved config snapshot, for reproducibility


class RunFinished(BaseEvent):
    event_type: Literal["run.finished"] = "run.finished"
    summary: dict          # simulator-side metrics (served, denied, wait stats)


# ----- vehicles -------------------------------------------------------------- #
class BusDepartedTerminal(VehicleEvent):
    event_type: Literal["vehicle.departed_terminal"] = "vehicle.departed_terminal"
    route_id: str
    trip_id: str
    scheduled_minute: float      # when it should have left
    interval_minutes: float      # interval in force at departure
    occupancy: int


class BusArrivedStop(VehicleEvent):
    event_type: Literal["vehicle.arrived_stop"] = "vehicle.arrived_stop"
    route_id: str
    trip_id: str
    stop_id: str
    stop_index: int
    occupancy: int               # on board when the doors open


class BusDepartedStop(VehicleEvent):
    event_type: Literal["vehicle.departed_stop"] = "vehicle.departed_stop"
    route_id: str
    trip_id: str
    stop_id: str
    stop_index: int
    occupancy: int               # on board when the doors close
    boarded: int
    alighted: int
    left_behind: int             # still waiting because the bus was full
    capacity: int


class BusCompletedTrip(VehicleEvent):
    event_type: Literal["vehicle.completed_trip"] = "vehicle.completed_trip"
    route_id: str
    trip_id: str
    round_trip_minutes: float


class BusDelayed(VehicleEvent):
    event_type: Literal["vehicle.delayed"] = "vehicle.delayed"
    route_id: str
    trip_id: str
    after_stop_id: str
    delay_minutes: float


class BusBrokeDown(VehicleEvent):
    event_type: Literal["vehicle.breakdown"] = "vehicle.breakdown"
    route_id: str
    trip_id: str
    after_stop_id: str
    expected_minutes: float


class BusRecovered(VehicleEvent):
    event_type: Literal["vehicle.recovered"] = "vehicle.recovered"
    route_id: str
    trip_id: str


class BusAssigned(VehicleEvent):
    """Bus moved between a route and the reserve pool (route_id None = reserve)."""

    event_type: Literal["vehicle.assigned"] = "vehicle.assigned"
    from_route_id: str | None
    to_route_id: str | None
    reason: str


# ----- passengers ------------------------------------------------------------ #
class PassengerArrived(BaseEvent):
    event_type: Literal["passenger.arrived"] = "passenger.arrived"
    passenger_id: str
    route_id: str
    stop_id: str
    destination_stop_id: str


class PassengerBoarded(BaseEvent):
    event_type: Literal["passenger.boarded"] = "passenger.boarded"
    passenger_id: str
    route_id: str
    stop_id: str
    bus_id: str
    trip_id: str
    wait_minutes: float


class PassengerDenied(BaseEvent):
    """Passenger could not board because the bus was full (left behind)."""

    event_type: Literal["passenger.denied"] = "passenger.denied"
    passenger_id: str
    route_id: str
    stop_id: str
    bus_id: str
    trip_id: str


class PassengerAlighted(BaseEvent):
    event_type: Literal["passenger.alighted"] = "passenger.alighted"
    passenger_id: str
    route_id: str
    stop_id: str
    bus_id: str
    trip_id: str
    travel_minutes: float


# ----- schedule -------------------------------------------------------------- #
class ScheduleApplied(BaseEvent):
    """The simulator started using a new schedule version for a route."""

    event_type: Literal["schedule.applied"] = "schedule.applied"
    route_id: str
    version: int
    interval_minutes: float
    buses_assigned: int
    recommendation_id: int | None = None


Event = Annotated[
    Union[
        RunStarted, RunFinished,
        BusDepartedTerminal, BusArrivedStop, BusDepartedStop, BusCompletedTrip,
        BusDelayed, BusBrokeDown, BusRecovered, BusAssigned,
        PassengerArrived, PassengerBoarded, PassengerDenied, PassengerAlighted,
        ScheduleApplied,
    ],
    Field(discriminator="event_type"),
]

_adapter: TypeAdapter = TypeAdapter(Event)

EVENT_TYPES: list[str] = sorted(
    m.model_fields["event_type"].default
    for m in BaseEvent.__subclasses__() + VehicleEvent.__subclasses__()
    if "event_type" in m.model_fields
)


def serialize(event: BaseEvent) -> bytes:
    return event.model_dump_json().encode()


def parse_event(body: bytes | str | dict) -> BaseEvent:
    """Parse raw message body into the right event class (raises ValidationError)."""
    if isinstance(body, dict):
        return _adapter.validate_python(body)
    return _adapter.validate_json(body)
