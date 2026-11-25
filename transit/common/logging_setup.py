"""Consistent log format across services (LOG_LEVEL env var controls verbosity)."""

import logging
import os


def setup_logging(service: str) -> logging.Logger:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format=f"%(asctime)s %(levelname)-7s [{service}] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("pika").setLevel(logging.WARNING)
    return logging.getLogger(service)
