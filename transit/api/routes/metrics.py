"""Summary metrics and system health.

GET /api/metrics   totals and per-route stats for the active run
GET /api/system    queue depth, dead letters, worker count, event rate, worker lag, simulator
                   and scheduler liveness: what you'd otherwise check in the RabbitMQ UI or logs
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from transit.api import queries, schemas
from transit.api.deps import get_session
from transit.api.routes.network import probe_dependency

router = APIRouter(tags=["metrics"])


@router.get("/metrics")
def metrics(session: Session = Depends(get_session)) -> dict:
    ctx = queries.RunContext(session)
    routes, totals = queries.routes_and_totals(ctx)
    return {"run": ctx.info(), "totals": totals, "routes": routes}


@router.get("/system", response_model=schemas.SystemHealth)
def system(session: Session = Depends(get_session), probe=Depends(probe_dependency)):
    ctx = queries.RunContext(session)
    return queries.system_health(ctx, probe, queries.utcnow())
