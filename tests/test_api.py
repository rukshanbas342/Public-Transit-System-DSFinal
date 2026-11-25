"""API tests (FastAPI TestClient, SQLite in memory).

Coverage: approve/decline require login; approving bumps route_schedules.version; read
endpoints return the schema in transit/api/schemas.py; the overview reflects worker data.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from transit.api import auth
from transit.api.broker import BrokerStats, QueueStats
from transit.api.deps import set_session_factory
from transit.api.main import app
from transit.api.routes.network import probe_dependency
from transit.common.config import load_simulation_config
from transit.common.models import (
    Base,
    OperatorDecision,
    PassengerJourney,
    ProcessedEvent,
    Recommendation,
    RouteSchedule,
    SimRun,
    StopVisit,
    VehicleState,
)
from transit.db.seed import seed

RUN = "run-api"


class FakeProbe:
    def stats(self):
        return BrokerStats(ok=True, events=QueueStats("transit.events.process", 42, 3),
                           dead=QueueStats("transit.events.dead", 0, 0))


@pytest.fixture
def factory(monkeypatch):
    monkeypatch.setenv("OPERATOR_USERNAME", "operator")
    monkeypatch.setenv("OPERATOR_PASSWORD", "secret")
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    sim = load_simulation_config("baseline", use_env=False)
    with factory() as s:
        seed(sim, s)
        s.add(SimRun(run_id=RUN, scenario="baseline", seed=42, config=sim.model_dump(mode="json")))
        s.add_all([
            PassengerJourney(passenger_id="p1", run_id=RUN, route_id="R1", origin_stop_id="R1-S1",
                             destination_stop_id="R1-S3", arrived_minute=100, boarded_minute=110),
            PassengerJourney(passenger_id="p2", run_id=RUN, route_id="R1", origin_stop_id="R1-S1",
                             destination_stop_id="R1-S3", arrived_minute=105, times_denied=1),
            StopVisit(event_id=uuid.uuid4(), run_id=RUN, route_id="R1", stop_id="R1-S1", vehicle_id="B01",
                      trip_id="t1", departed_minute=110, occupancy=20, capacity=40, boarded=1, alighted=0,
                      left_behind=1),
            ProcessedEvent(event_id=uuid.uuid4(), event_type="passenger.arrived", run_id=RUN),
            Recommendation(id=1, run_id=RUN, route_id="R1", created_minute=110, current_interval=20,
                           suggested_interval=10, buses_delta=3, demand_score=3.5, peak_label="peak",
                           est_wait_saving_minutes=5, reason="busy"),
        ])
        bus = s.get(VehicleState, "B01")
        bus.run_id, bus.route_id, bus.status, bus.stop_index, bus.occupancy, bus.last_sim_minute = \
            RUN, "R1", "at_stop", 0, 20, 112
        s.commit()
    set_session_factory(factory)
    app.dependency_overrides[probe_dependency] = lambda: FakeProbe()
    yield factory
    set_session_factory(None)
    app.dependency_overrides.clear()


@pytest.fixture
def client(factory):
    return TestClient(app)


def _login(client) -> dict:
    r = client.post("/api/login", json={"username": "operator", "password": "secret"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok"}


def test_login_rejects_wrong_password(client):
    assert client.post("/api/login", json={"username": "operator", "password": "nope"}).status_code == 401


def test_tokens_are_signed_and_expire():
    token, _ = auth.create_token("operator", ttl=60, now=1000)
    assert auth.verify_token(token, now=1030) == "operator"
    assert auth.verify_token(token, now=1100) is None
    payload, sig = token.split(".")
    assert auth.verify_token(f"{payload}x.{sig}", now=1030) is None


def test_approve_and_decline_require_login(client):
    assert client.post("/api/recommendations/1/approve").status_code == 401
    assert client.post("/api/recommendations/1/decline",
                       headers={"Authorization": "Bearer forged.token"}).status_code == 401


def test_approve_bumps_schedule_version(client, factory):
    r = client.post("/api/recommendations/1/approve", headers=_login(client))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recommendation"]["status"] == "approved"
    assert body["recommendation"]["decided_by"] == "operator"
    assert body["schedule_version"] == 2
    with factory() as s:
        sched = s.get(RouteSchedule, "R1")
        assert (sched.version, sched.interval_minutes, sched.buses_assigned, sched.source) == (2, 10, 6, "operator")
        assert s.query(OperatorDecision).one().decided_minute == 112
    # second decision on the same recommendation is a conflict
    assert client.post("/api/recommendations/1/decline", headers=_login(client)).status_code == 409


def test_decline(client, factory):
    r = client.post("/api/recommendations/1/decline", headers=_login(client))
    assert r.status_code == 200 and r.json()["recommendation"]["status"] == "declined"
    with factory() as s:
        assert s.get(RouteSchedule, "R1").version == 1


def test_unknown_recommendation_is_404(client):
    assert client.post("/api/recommendations/99/approve", headers=_login(client)).status_code == 404


def test_overview_reports_network_and_system(client):
    data = client.get("/api/overview").json()
    assert data["run"]["run_id"] == RUN and data["run"]["clock"] == "06:52"   # 05:00 + 112 min
    r1 = next(r for r in data["routes"] if r["route_id"] == "R1")
    assert (r1["arrived"], r1["served"], r1["waiting"], r1["left_behind"]) == (2, 1, 1, 1)
    assert r1["avg_wait"] == 10 and r1["avg_occupancy_ratio"] == 0.5
    assert data["totals"]["arrived"] == 2
    b01 = next(v for v in data["vehicles"] if v["vehicle_id"] == "B01")
    assert b01["route_id"] == "R1" and b01["occupancy"] == 20 and b01["age_seconds"] is not None
    stop = next(s for s in data["stops"] if s["stop_id"] == "R1-S1")
    assert stop["waiting"] == 1
    assert [p["id"] for p in data["pending"]] == [1]
    system = data["system"]
    assert system["workers"] == 3 and system["events_queue"]["messages"] == 42
    assert system["events_processed"] == 1


def test_read_endpoints_match_schema(client):
    assert len(client.get("/api/routes").json()) == 3
    assert len(client.get("/api/vehicles").json()) == 12
    assert len(client.get("/api/stops").json()) == 24
    assert client.get("/api/recommendations?status=pending").json()[0]["route_id"] == "R1"
    assert client.get("/api/metrics").json()["totals"]["served"] == 1
    assert client.get("/api/system").json()["broker_ok"] is True


def test_empty_database_still_renders(factory, client):
    with factory() as s:
        s.query(Recommendation).delete()
        s.query(SimRun).delete()
        s.commit()
    data = client.get("/api/overview").json()
    assert data["run"] is None and data["pending"] == [] and len(data["routes"]) == 3


def test_broker_probe_retries_once_after_a_dropped_connection():
    from transit.api.broker import BrokerProbe

    probe = BrokerProbe("amqp://unused")
    answers = iter([BrokerStats(ok=False, error="connection reset"), FakeProbe().stats()])
    probe._query = lambda: next(answers)
    assert probe.stats().ok and probe.stats().events.consumers == 3   # second call is cached
