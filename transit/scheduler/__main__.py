"""Entry point: python -m transit.scheduler

Loads config/scheduler.yaml and checks the simulated clock every `evaluation.poll_seconds`
(real time). Every `evaluation.every_minutes` of SIMULATED time it evaluates every route.
Simulated time = the latest sim_minute seen in the database for the active run.

AUTO_APPROVE=true (env) or operator.auto_approve in the YAML applies recommendations directly.
"""

from __future__ import annotations

import os
import signal
import threading

from sqlalchemy.exc import SQLAlchemyError

from transit.common.config import load_scheduler_config
from transit.common.db import get_sessionmaker, wait_for_db
from transit.common.logging_setup import setup_logging
from transit.scheduler.service import SchedulerService


def _env_flag(name: str) -> bool | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return None
    return value.strip().lower() in ("1", "true", "yes", "on")


def main() -> int:
    log = setup_logging("scheduler")
    cfg = load_scheduler_config()
    env_auto = _env_flag("AUTO_APPROVE")
    auto_approve = cfg.operator.auto_approve if env_auto is None else env_auto

    wait_for_db()
    service = SchedulerService(cfg, get_sessionmaker(), auto_approve)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda signum, _frame: (log.info("received signal %s, stopping", signum), stop.set()))

    log.info("scheduler started: every %.0f sim min over a %.0f min window, auto_approve=%s",
             cfg.evaluation.every_minutes, cfg.evaluation.window_minutes, auto_approve)
    while not stop.is_set():
        try:
            result = service.tick()
            if result is not None:
                log.debug("evaluated %s at sim minute %.0f: %d new, %d superseded", result.run_id,
                          result.sim_minute, len(result.created), len(result.superseded))
        except SQLAlchemyError as exc:
            log.warning("evaluation failed, retrying: %s", exc)
        stop.wait(cfg.evaluation.poll_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
