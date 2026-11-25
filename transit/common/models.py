"""Database schema (SQLAlchemy 2.0). Alembic migrations are generated from this.

Who writes what (the contract between services):

    table                     written by            read by
    ------------------------  --------------------  ---------------------------
    routes, stops, vehicles   seed                  everyone
    historical_demand         seed                  scheduler
    operators                 seed / api            api (auth)
    route_schedules           seed, api (approve),  simulator (polls), dashboard
                              scheduler (auto-approve)
    sim_runs                  worker (run.* events) experiments, dashboard
    processed_events          worker                worker (dedup)
    vehicle_state             worker                dashboard, scheduler
    passenger_journeys        worker                scheduler, experiments
    stop_visits               worker                scheduler, dashboard
    recommendations           scheduler, api        dashboard, api, worker (applied)
    operator_decisions        api / scheduler       dashboard, report
    route_scheduler_state     scheduler             scheduler, dashboard

Time columns named *_minute are SIMULATED minutes since service start; columns
named *_at are wall-clock timestamps.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------- #
# Static network (seeded from config/simulation.yaml)
# --------------------------------------------------------------------------- #
class Route(Base):
    __tablename__ = "routes"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    popularity: Mapped[float] = mapped_column(Float, default=1.0)
    travel_minutes: Mapped[float] = mapped_column(Float)  # one loop, excluding dwell

    stops: Mapped[list["Stop"]] = relationship(back_populates="route", order_by="Stop.stop_index")


class Stop(Base):
    __tablename__ = "stops"
    __table_args__ = (UniqueConstraint("route_id", "stop_index"),)

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    route_id: Mapped[str] = mapped_column(ForeignKey("routes.id"))
    stop_index: Mapped[int] = mapped_column(Integer)  # 0 = terminal
    name: Mapped[str] = mapped_column(String(100))
    travel_to_next: Mapped[float] = mapped_column(Float)
    weight: Mapped[float] = mapped_column(Float, default=1.0)

    route: Mapped[Route] = relationship(back_populates="stops")


class Vehicle(Base):
    __tablename__ = "vehicles"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)  # "B01"
    capacity: Mapped[int] = mapped_column(Integer)


class HistoricalDemand(Base):
    """Typical passengers/hour per route and hour of day: the route's baseline ("popularity")."""

    __tablename__ = "historical_demand"

    route_id: Mapped[str] = mapped_column(ForeignKey("routes.id"), primary_key=True)
    hour_of_day: Mapped[int] = mapped_column(Integer, primary_key=True)
    passengers_per_hour: Mapped[float] = mapped_column(Float)


class Operator(Base):
    __tablename__ = "operators"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --------------------------------------------------------------------------- #
# The schedule the simulator follows (control loop: approve -> simulator)
# --------------------------------------------------------------------------- #
class RouteSchedule(Base):
    """Current schedule per route. The simulator polls this table.

    To change service on a route, UPDATE this row and increment `version`.
    The simulator applies a new version at the route's next terminal departure
    and emits a schedule.applied event.
    """

    __tablename__ = "route_schedules"

    route_id: Mapped[str] = mapped_column(ForeignKey("routes.id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    interval_minutes: Mapped[float] = mapped_column(Float)
    buses_assigned: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(20), default="seed")  # seed | operator | auto
    recommendation_id: Mapped[int | None] = mapped_column(ForeignKey("recommendations.id"), nullable=True)
    applied_version: Mapped[int | None] = mapped_column(Integer, nullable=True)  # set by worker on schedule.applied
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# --------------------------------------------------------------------------- #
# Event processing state (written by workers)
# --------------------------------------------------------------------------- #
class SimRun(Base):
    __tablename__ = "sim_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scenario: Mapped[str] = mapped_column(String(50))
    seed: Mapped[int] = mapped_column(Integer)
    config: Mapped[dict] = mapped_column(JSON)
    summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProcessedEvent(Base):
    """Idempotency log: insert event_id in the SAME transaction as the update it caused."""

    __tablename__ = "processed_events"

    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(40))
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VehicleState(Base):
    """Latest known state per bus. Only update when event.seq > last_seq."""

    __tablename__ = "vehicle_state"

    vehicle_id: Mapped[str] = mapped_column(ForeignKey("vehicles.id"), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    route_id: Mapped[str | None] = mapped_column(ForeignKey("routes.id"), nullable=True)  # None = reserve
    trip_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stop_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    stop_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="idle")  # idle|in_service|at_stop|broken_down|deadhead
    occupancy: Mapped[int] = mapped_column(Integer, default=0)
    last_seq: Mapped[int] = mapped_column(Integer, default=0)
    last_sim_minute: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PassengerJourney(Base):
    __tablename__ = "passenger_journeys"
    __table_args__ = (
        Index("ix_journeys_run_route_arrived", "run_id", "route_id", "arrived_minute"),
    )

    passenger_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64))
    route_id: Mapped[str] = mapped_column(ForeignKey("routes.id"))
    origin_stop_id: Mapped[str] = mapped_column(ForeignKey("stops.id"))
    destination_stop_id: Mapped[str] = mapped_column(ForeignKey("stops.id"))
    arrived_minute: Mapped[float] = mapped_column(Float)
    boarded_minute: Mapped[float | None] = mapped_column(Float, nullable=True)
    alighted_minute: Mapped[float | None] = mapped_column(Float, nullable=True)
    vehicle_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    times_denied: Mapped[int] = mapped_column(Integer, default=0)


class StopVisit(Base):
    """One row per bus departing a stop: occupancy, boardings and left-behind counts."""

    __tablename__ = "stop_visits"
    __table_args__ = (Index("ix_visits_run_route_minute", "run_id", "route_id", "departed_minute"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True)
    run_id: Mapped[str] = mapped_column(String(64))
    route_id: Mapped[str] = mapped_column(ForeignKey("routes.id"))
    stop_id: Mapped[str] = mapped_column(ForeignKey("stops.id"))
    vehicle_id: Mapped[str] = mapped_column(ForeignKey("vehicles.id"))
    trip_id: Mapped[str] = mapped_column(String(64))
    departed_minute: Mapped[float] = mapped_column(Float)
    occupancy: Mapped[int] = mapped_column(Integer)
    capacity: Mapped[int] = mapped_column(Integer)
    boarded: Mapped[int] = mapped_column(Integer)
    alighted: Mapped[int] = mapped_column(Integer)
    left_behind: Mapped[int] = mapped_column(Integer)


# --------------------------------------------------------------------------- #
# Scheduling
# --------------------------------------------------------------------------- #
class Recommendation(Base):
    __tablename__ = "recommendations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    route_id: Mapped[str] = mapped_column(ForeignKey("routes.id"))
    created_minute: Mapped[float] = mapped_column(Float)
    current_interval: Mapped[float] = mapped_column(Float)
    suggested_interval: Mapped[float] = mapped_column(Float)
    buses_delta: Mapped[int] = mapped_column(Integer, default=0)  # + add, - remove
    reallocate_from_route_id: Mapped[str | None] = mapped_column(ForeignKey("routes.id"), nullable=True)
    demand_score: Mapped[float] = mapped_column(Float)
    peak_label: Mapped[str] = mapped_column(String(10))  # peak | normal | off_peak
    est_wait_saving_minutes: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(Text)
    warning: Mapped[str | None] = mapped_column(Text, nullable=True)  # e.g. "no buses available"
    status: Mapped[str] = mapped_column(String(20), default="pending")
    # pending | approved | declined | auto_approved | applied | superseded
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OperatorDecision(Base):
    __tablename__ = "operator_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recommendation_id: Mapped[int] = mapped_column(ForeignKey("recommendations.id"))
    decision: Mapped[str] = mapped_column(String(20))  # approved | declined | auto_approved
    operator: Mapped[str] = mapped_column(String(50))  # username or "auto"
    decided_minute: Mapped[float | None] = mapped_column(Float, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RouteSchedulerState(Base):
    """Scheduler memory per route, so it can resume after a restart."""

    __tablename__ = "route_scheduler_state"

    route_id: Mapped[str] = mapped_column(ForeignKey("routes.id"), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ema_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    peak_label: Mapped[str] = mapped_column(String(10), default="normal")
    cooldown_until_minute: Mapped[float | None] = mapped_column(Float, nullable=True)
    suppressed_until_minute: Mapped[float | None] = mapped_column(Float, nullable=True)
    suppressed_score: Mapped[float | None] = mapped_column(Float, nullable=True)  # score when declined
    last_evaluated_minute: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
