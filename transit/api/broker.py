"""Queue depth and consumer counts from RabbitMQ, for the dashboard's system panel.

Uses a passive queue_declare (no management plugin needed): it returns the number of ready
messages and consumers. On connecting it runs the shared, idempotent declare_topology first. Results are cached for
CACHE_SECONDS so many open dashboards don't hammer the broker.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import pika
from pika.exceptions import AMQPError

from transit.common.config import InfraSettings
from transit.common.messaging import DEAD_LETTER_QUEUE, EVENTS_QUEUE, declare_topology

log = logging.getLogger(__name__)
CACHE_SECONDS = 2.0


@dataclass(frozen=True)
class QueueStats:
    name: str
    messages: int
    consumers: int


@dataclass(frozen=True)
class BrokerStats:
    ok: bool
    events: QueueStats | None = None
    dead: QueueStats | None = None
    error: str | None = None


class BrokerProbe:
    def __init__(self, url: str | None = None):
        self._url = url or InfraSettings.from_env().rabbitmq_url
        self._lock = threading.Lock()
        self._conn: pika.BlockingConnection | None = None
        self._cached: tuple[float, BrokerStats] | None = None

    def stats(self) -> BrokerStats:
        with self._lock:
            if self._cached and time.monotonic() - self._cached[0] < CACHE_SECONDS:
                return self._cached[1]
            result = self._query()
            if not result.ok:          # a dropped idle connection: retry once on a fresh one
                result = self._query()
            self._cached = (time.monotonic(), result)
            return result

    def _query(self) -> BrokerStats:
        try:
            if self._conn is None or self._conn.is_closed:
                params = pika.URLParameters(self._url)
                params.socket_timeout = 2
                params.blocked_connection_timeout = 2
                params.connection_attempts = 1
                # The probe is idle between dashboard polls (or for hours with no dashboard
                # open) and BlockingConnection only answers heartbeats while it's in use, so
                # the broker would drop it after 60 s. Heartbeats off for this read-only link.
                params.heartbeat = 0
                self._conn = pika.BlockingConnection(params)
                # Same idempotent setup the simulator and worker run, so the passive declares
                # below never hit "no queue" when the dashboard is opened before they start.
                setup = self._conn.channel()
                declare_topology(setup)
                setup.close()
            queues = []
            for name in (EVENTS_QUEUE, DEAD_LETTER_QUEUE):
                channel = self._conn.channel()   # a failed passive declare closes the channel
                try:
                    method = channel.queue_declare(name, passive=True).method
                    queues.append(QueueStats(name, method.message_count, method.consumer_count))
                finally:
                    if channel.is_open:
                        channel.close()
            return BrokerStats(ok=True, events=queues[0], dead=queues[1])
        except (AMQPError, OSError) as exc:
            self._close()
            message = str(exc) or type(exc).__name__
            return BrokerStats(ok=False, error=message)

    def _close(self) -> None:
        try:
            if self._conn is not None and self._conn.is_open:
                self._conn.close()
        except AMQPError:
            pass
        self._conn = None


_probe: BrokerProbe | None = None


def get_probe() -> BrokerProbe:
    global _probe
    if _probe is None:
        _probe = BrokerProbe()
    return _probe
