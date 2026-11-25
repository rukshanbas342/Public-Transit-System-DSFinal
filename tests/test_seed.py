"""Seed tests (SQLite in memory): resets keep versions increasing and never happen mid-run."""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from transit.common.config import load_simulation_config
from transit.common.models import Base, RouteSchedule
from transit.db import seed as seed_module


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def cfg():
    return load_simulation_config("baseline", use_env=False)


def _schedules(session):
    return {s.route_id: (s.version, s.interval_minutes) for s in session.scalars(select(RouteSchedule))}


def _approve_r1(session):
    r1 = session.get(RouteSchedule, "R1")
    r1.version, r1.interval_minutes = r1.version + 5, 9.0
    session.flush()


def test_reset_restores_initial_schedule_with_a_higher_version(session, cfg):
    seed_module.seed(cfg, session)
    assert _schedules(session)["R1"] == (1, 20)
    _approve_r1(session)                      # R1 now at version 6, 9 min
    seed_module.seed(cfg, session)
    assert _schedules(session)["R1"] == (7, 20)   # never back to 1: a running simulator would ignore it
    assert _schedules(session)["R2"] == (2, 20)


def test_reset_is_skipped_while_a_simulator_is_running(session, cfg, monkeypatch):
    seed_module.seed(cfg, session)
    _approve_r1(session)
    monkeypatch.setattr(seed_module, "simulator_connected", lambda s: True)
    seed_module.seed(cfg, session)
    assert _schedules(session)["R1"] == (6, 9.0)  # approved schedule survives the rerun

    seed_module.seed(cfg, session, force_reset=True)
    assert _schedules(session)["R1"] == (7, 20)


def test_simulator_detection_is_off_outside_postgres(session):
    assert seed_module.simulator_connected(session) is False
