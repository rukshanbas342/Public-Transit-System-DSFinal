"""Recommendation endpoints: the operator's side of the control loop.

GET  /api/recommendations?status=pending
POST /api/recommendations/{id}/approve   (login required)
POST /api/recommendations/{id}/decline   (login required)

Approve and decline reuse transit.scheduler.service so the API and auto-approve behave the
same: one transaction sets the status, writes operator_decisions and (approve) updates
route_schedules with version + 1. A conflict (not pending, or the buses are gone) is HTTP 409.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from transit.api import queries, schemas
from transit.api.auth import require_operator
from transit.api.deps import get_session
from transit.common.models import OperatorDecision, Recommendation, RouteSchedule
from transit.scheduler.service import RecommendationError, approve_recommendation, decline_recommendation

router = APIRouter(prefix="/recommendations", tags=["recommendations"])


@router.get("", response_model=list[schemas.Recommendation])
def list_recommendations(status_filter: str | None = Query(None, alias="status"), limit: int = 50,
                         session: Session = Depends(get_session)):
    statuses = [s.strip() for s in status_filter.split(",")] if status_filter else None
    return queries.recommendations(queries.RunContext(session), statuses, limit=min(limit, 500))


def _decide(rec_id: int, approve: bool, session: Session, operator: str) -> schemas.DecisionResponse:
    rec = session.scalar(select(Recommendation).where(Recommendation.id == rec_id).with_for_update())
    if rec is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"recommendation {rec_id} not found")
    ctx = queries.RunContext(session)
    try:
        if approve:
            approve_recommendation(session, rec, operator=operator, decided_minute=ctx.sim_minute)
        else:
            decline_recommendation(session, rec, operator=operator, decided_minute=ctx.sim_minute)
        session.commit()
    except RecommendationError as exc:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    decision = session.scalar(select(OperatorDecision).where(OperatorDecision.recommendation_id == rec.id)
                              .order_by(OperatorDecision.id.desc()))
    schedule = session.get(RouteSchedule, rec.route_id)
    return schemas.DecisionResponse(recommendation=queries.to_schema(ctx, rec, decision),
                                    schedule_version=schedule.version if schedule else None)


@router.post("/{rec_id}/approve", response_model=schemas.DecisionResponse)
def approve(rec_id: int, session: Session = Depends(get_session), operator: str = Depends(require_operator)):
    return _decide(rec_id, True, session, operator)


@router.post("/{rec_id}/decline", response_model=schemas.DecisionResponse)
def decline(rec_id: int, session: Session = Depends(get_session), operator: str = Depends(require_operator)):
    return _decide(rec_id, False, session, operator)
