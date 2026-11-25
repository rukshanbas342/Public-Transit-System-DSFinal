"""Entry point for ``python -m transit.worker``."""

from transit.common.db import wait_for_db
from transit.common.logging_setup import setup_logging
from transit.worker.consumer import run_worker


def main() -> None:
    setup_logging("worker")
    wait_for_db()
    run_worker()


if __name__ == "__main__":
    main()
