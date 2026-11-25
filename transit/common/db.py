"""Database engine and session helpers shared by all services."""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from transit.common.config import InfraSettings

log = logging.getLogger(__name__)


SIMULATOR_APP_NAME = "transit-simulator"   # the seed checks for this connection before resetting


@lru_cache
def get_engine(url: str | None = None, application_name: str | None = None) -> Engine:
    """application_name tags the connection in pg_stat_activity (Postgres only)."""
    url = url or InfraSettings.from_env().database_url
    connect_args = {"application_name": application_name} if application_name else {}
    return create_engine(url, pool_pre_ping=True, future=True, connect_args=connect_args)


def get_sessionmaker(url: str | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(url), expire_on_commit=False)


@contextmanager
def session_scope(url: str | None = None) -> Iterator[Session]:
    """One transaction: commit on success, roll back on any exception."""
    session = get_sessionmaker(url)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def wait_for_db(url: str | None = None, retries: int = 30, delay: float = 2.0) -> None:
    engine = get_engine(url)
    for attempt in range(1, retries + 1):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except OperationalError as exc:
            log.warning("database not ready (attempt %d/%d): %s", attempt, retries, exc.orig)
            time.sleep(delay)
    raise ConnectionError("database did not become ready")
