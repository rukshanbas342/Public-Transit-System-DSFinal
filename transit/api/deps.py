"""Shared FastAPI dependencies."""

from __future__ import annotations

from typing import Iterator

from sqlalchemy.orm import Session, sessionmaker

from transit.common.db import get_sessionmaker

_factory: sessionmaker[Session] | None = None


def set_session_factory(factory: sessionmaker[Session] | None) -> None:
    """Tests point the API at their own database."""
    global _factory
    _factory = factory


def get_session() -> Iterator[Session]:
    factory = _factory or get_sessionmaker()
    session = factory()
    try:
        yield session
    finally:
        session.close()
