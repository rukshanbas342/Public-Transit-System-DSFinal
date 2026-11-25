"""Pydantic response/request models for the API (the contract with the dashboard).

Keep the TypeScript types in dashboard/src/api/types.ts in sync with this file.
Times: *_minute = simulated minutes since service start; *_seconds = real seconds.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    username: str
    expires_at: datetime


class RunInfo(BaseModel):
    run_id: str
    scenario: str
    seed: int
    service_start: str
    service_end: str
    started_at: datetime
    finished_at: datetime | None
    sim_minute: float | None        # latest simulated minute seen in the database
    clock: str | None               # sim_minute as time of day, "HH:MM"
    duration_minutes: int
    summary: dict | None            # simulator's own end-of-run metrics (run.finished)


class RouteStatus(BaseModel):
    route_id: str
    name: str
    round_trip_minutes: float
    interval_minutes: float
    buses_assigned: int
    buses_on_route: int             # vehicle_state rows currently assigned to the route
    schedule_version: int
    applied_version: int | None     # what the simulator has confirmed (schedule.applied)
    schedule_source: str
    peak_label: str
    demand_score: float | None
    cooldown_until_minute: float | None
    arrived: int
    served: int                     # boarded a bus
    waiting: int                    # arrived, not boarded yet
    left_behind: int                # passengers denied boarding at least once
    avg_wait: float | None          # minutes, passengers who boarded
    p95_wait: float | None
    recent_avg_wait: float | None   # passengers who boarded in the last 60 sim minutes
    avg_occupancy_ratio: float | None


class VehicleStatus(BaseModel):
    vehicle_id: str
    route_id: str | None            # None = reserve
    status: str
    stop_id: str | None
    stop_index: int | None
    occupancy: int
    capacity: int
    last_sim_minute: float | None
    updated_at: datetime | None
    age_seconds: float | None       # real seconds since the worker last updated this bus


class StopStatus(BaseModel):
    stop_id: str
    route_id: str
    stop_index: int
    name: str
    waiting: int


class Recommendation(BaseModel):
    id: int
    route_id: str
    created_minute: float
    created_clock: str | None
    current_interval: float
    suggested_interval: float
    buses_delta: int
    reallocate_from_route_id: str | None
    demand_score: float
    peak_label: str
    est_wait_saving_minutes: float
    reason: str
    warning: str | None
    status: str
    decided_by: str | None
    decided_minute: float | None


class QueueStatus(BaseModel):
    name: str
    messages: int
    consumers: int


class SystemHealth(BaseModel):
    database_ok: bool
    broker_ok: bool
    broker_error: str | None
    events_queue: QueueStatus | None
    dead_letter_queue: QueueStatus | None
    workers: int | None                       # consumers on the events queue
    simulator_connected: bool | None          # None = can't tell (not Postgres)
    events_processed: int                     # in the active run
    events_per_second: float                  # over the last 10 real seconds
    last_event_age_seconds: float | None      # since the worker last committed an event
    scheduler_last_evaluated_minute: float | None
    scheduler_age_seconds: float | None       # since the scheduler last wrote its state
    auto_approve_hint: bool                   # any auto-approved recommendations this run


class Totals(BaseModel):
    arrived: int
    served: int
    waiting: int
    left_behind: int
    avg_wait: float | None
    p95_wait: float | None


class Overview(BaseModel):
    """Everything the dashboard shows, in one poll."""

    server_time: datetime
    run: RunInfo | None
    totals: Totals | None
    routes: list[RouteStatus]
    vehicles: list[VehicleStatus]
    stops: list[StopStatus]
    pending: list[Recommendation]
    history: list[Recommendation]
    system: SystemHealth


class DecisionResponse(BaseModel):
    recommendation: Recommendation
    schedule_version: int | None
