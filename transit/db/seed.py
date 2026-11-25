"""Seed the database from config/simulation.yaml.

    python -m transit.db.seed [--scenario NAME] [--set key=value ...] [--no-reset]

Idempotent: rerunning updates rows in place. By default it also RESETS the
dynamic state (route schedules back to the configured initial interval,
vehicle state to idle, scheduler state cleared) so every `docker compose up`
starts a clean run. Event history (journeys, visits, recommendations) is kept,
separated by run_id.

The reset is SKIPPED while a simulator is connected to the database, because
Compose reruns this step whenever a dependent service starts (for example
`docker compose start worker` in the fault-tolerance demo), and resetting mid-run
would undo approved schedules. `--force-reset` overrides that. Schedule versions
only ever increase, so a reset is always seen as newer by a running simulator.
"""

from __future__ import annotations

import argparse
import logging
import os

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from transit.common.config import SimulationConfig, load_simulation_config
from transit.common.db import SIMULATOR_APP_NAME, session_scope, wait_for_db
from transit.common.logging_setup import setup_logging
from transit.common.models import (
    HistoricalDemand,
    Operator,
    Route,
    RouteSchedule,
    RouteSchedulerState,
    Stop,
    Vehicle,
    VehicleState,
)
from transit.common.security import hash_password

log = logging.getLogger(__name__)


def _upsert(session: Session, model, rows: list[dict], keys: list[str]) -> None:
    if not rows:
        return
    stmt = insert(model).values(rows)
    update_cols = {c: stmt.excluded[c] for c in rows[0] if c not in keys}
    stmt = stmt.on_conflict_do_update(index_elements=keys, set_=update_cols) if update_cols \
        else stmt.on_conflict_do_nothing(index_elements=keys)
    session.execute(stmt)


def expected_passengers_per_hour(cfg: SimulationConfig, route_id: str, hour: int) -> float:
    """Average demand the config implies for a route and hour (no surges) - the historical baseline."""
    route = cfg.route(route_id)
    per_minute = cfg.demand.base_rate_per_stop * route.popularity * sum(s.weight for s in route.stops)
    return round(per_minute * cfg.demand.hourly_profile[hour] * 60, 2)


def simulator_connected(session: Session) -> bool:
    """True if a simulator following the database schedule is running right now (Postgres only)."""
    if session.get_bind().dialect.name != "postgresql":
        return False
    return bool(session.scalar(
        text("SELECT count(*) FROM pg_stat_activity WHERE application_name = :name AND pid <> pg_backend_pid()"),
        {"name": SIMULATOR_APP_NAME}))


def seed(cfg: SimulationConfig, session: Session, reset: bool = True, force_reset: bool = False) -> None:
    _upsert(session, Route, [
        {"id": r.id, "name": r.name, "popularity": r.popularity, "travel_minutes": r.travel_minutes}
        for r in cfg.routes
    ], ["id"])
    _upsert(session, Stop, [
        {"id": s.id, "route_id": r.id, "stop_index": i, "name": s.name,
         "travel_to_next": s.travel_to_next, "weight": s.weight}
        for r in cfg.routes for i, s in enumerate(r.stops)
    ], ["id"])
    _upsert(session, Vehicle, [{"id": b, "capacity": cfg.fleet.capacity} for b in cfg.bus_ids], ["id"])
    _upsert(session, HistoricalDemand, [
        {"route_id": r.id, "hour_of_day": h, "passengers_per_hour": expected_passengers_per_hour(cfg, r.id, h)}
        for r in cfg.routes for h in range(24)
    ], ["route_id", "hour_of_day"])

    username = os.environ.get("OPERATOR_USERNAME", "operator")
    password = os.environ.get("OPERATOR_PASSWORD", "operator")
    _upsert(session, Operator, [{"username": username, "password_hash": hash_password(password)}], ["username"])

    if reset and not force_reset and simulator_connected(session):
        log.warning("a simulator is running: keeping current schedules and vehicle state "
                    "(use --force-reset to reset anyway)")
        reset = False

    if reset:
        old_versions = dict(session.execute(select(RouteSchedule.route_id, RouteSchedule.version)).all())
        session.execute(delete(RouteSchedulerState))
        session.execute(delete(RouteSchedule))
        session.flush()
        session.add_all(
            RouteSchedule(route_id=r.id, version=old_versions.get(r.id, 0) + 1,
                          interval_minutes=r.initial_interval, buses_assigned=r.initial_buses, source="seed")
            for r in cfg.routes
        )
        session.execute(delete(VehicleState))
        session.flush()
        session.add_all(VehicleState(vehicle_id=b, status="idle", occupancy=0, last_seq=0) for b in cfg.bus_ids)

    log.info("seeded %d routes, %d stops, %d buses (reset=%s)",
             len(cfg.routes), sum(len(r.stops) for r in cfg.routes), cfg.fleet.size, reset)


def main() -> None:
    setup_logging("seed")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario")
    parser.add_argument("--set", dest="overrides", action="append", default=[])
    parser.add_argument("--no-reset", action="store_true", help="keep current schedules and vehicle state")
    parser.add_argument("--force-reset", action="store_true", help="reset even while a simulator is running")
    args = parser.parse_args()

    cfg = load_simulation_config(args.scenario, args.overrides)
    wait_for_db()
    with session_scope() as session:
        seed(cfg, session, reset=not args.no_reset, force_reset=args.force_reset)


if __name__ == "__main__":
    main()
