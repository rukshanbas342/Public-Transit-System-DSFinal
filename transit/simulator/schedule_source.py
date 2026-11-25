"""Where the simulator gets its schedule from.

The simulator polls a ScheduleSource every `time.schedule_poll_minutes`. A
source returns the CURRENT schedule per route; the simulator compares the
version number with what it is running and, if newer, applies it at that
route's next terminal departure.

    FixedScheduleSource      the configured initial schedule, never changes (baseline runs)
    DatabaseScheduleSource   reads route_schedules (live system: approvals flow in here)
    CallbackScheduleSource   any Python function (headless experiments with an in-process scheduler)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Protocol

from transit.common.config import SimulationConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScheduleEntry:
    route_id: str
    version: int
    interval_minutes: float
    buses_assigned: int
    recommendation_id: int | None = None


class ScheduleSource(Protocol):
    def poll(self, sim_minute: float) -> dict[str, ScheduleEntry]: ...


def initial_schedule(cfg: SimulationConfig) -> dict[str, ScheduleEntry]:
    return {
        r.id: ScheduleEntry(r.id, 1, r.initial_interval, r.initial_buses)
        for r in cfg.routes
    }


class FixedScheduleSource:
    def __init__(self, cfg: SimulationConfig):
        self._schedule = initial_schedule(cfg)

    def poll(self, sim_minute: float) -> dict[str, ScheduleEntry]:
        return self._schedule


class CallbackScheduleSource:
    def __init__(self, fn: Callable[[float], dict[str, ScheduleEntry]]):
        self._fn = fn

    def poll(self, sim_minute: float) -> dict[str, ScheduleEntry]:
        return self._fn(sim_minute)


class DatabaseScheduleSource:
    """Reads route_schedules. If the DB is briefly unavailable, keeps the last schedule."""

    def __init__(self, database_url: str | None = None):
        from transit.common.db import SIMULATOR_APP_NAME, get_engine

        # Tagged so the seed can tell a run is in progress and must not reset schedules.
        self._engine = get_engine(database_url, SIMULATOR_APP_NAME)
        self._last: dict[str, ScheduleEntry] = {}

    def poll(self, sim_minute: float) -> dict[str, ScheduleEntry]:
        from sqlalchemy import select
        from sqlalchemy.exc import SQLAlchemyError

        from transit.common.models import RouteSchedule

        try:
            with self._engine.connect() as conn:
                rows = conn.execute(select(
                    RouteSchedule.route_id, RouteSchedule.version, RouteSchedule.interval_minutes,
                    RouteSchedule.buses_assigned, RouteSchedule.recommendation_id,
                )).all()
            self._last = {r.route_id: ScheduleEntry(*r) for r in rows}
        except SQLAlchemyError as exc:
            log.warning("schedule poll failed, keeping last schedule: %s", exc)
        return self._last
