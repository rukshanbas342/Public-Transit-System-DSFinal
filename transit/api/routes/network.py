"""Read endpoints for the live network (active run = latest sim_runs row).

GET /api/overview   everything the dashboard shows, in one call (what it polls every 1.5 s)
GET /api/run        run id, scenario, simulated clock
GET /api/routes     current schedule, peak label, demand score and wait stats per route
GET /api/vehicles   position, occupancy, status, and age of the last update (stale data is visible)
GET /api/stops      passengers waiting at each stop
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from transit.api import queries, schemas
from transit.api.broker import get_probe
from transit.api.deps import get_session

router = APIRouter(tags=["network"])


def probe_dependency():
    return get_probe()


@router.get("/overview", response_model=schemas.Overview)
def overview(session: Session = Depends(get_session), probe=Depends(probe_dependency)) -> schemas.Overview:
    return queries.overview(session, probe)


@router.get("/run", response_model=schemas.RunInfo | None)
def run(session: Session = Depends(get_session)):
    return queries.RunContext(session).info()


@router.get("/routes", response_model=list[schemas.RouteStatus])
def routes(session: Session = Depends(get_session)):
    return queries.routes_and_totals(queries.RunContext(session))[0]


@router.get("/vehicles", response_model=list[schemas.VehicleStatus])
def vehicles(session: Session = Depends(get_session)):
    return queries.vehicles(queries.RunContext(session), queries.utcnow())


@router.get("/stops", response_model=list[schemas.StopStatus])
def stops(session: Session = Depends(get_session)):
    return queries.stops(queries.RunContext(session))
