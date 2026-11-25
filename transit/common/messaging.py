"""RabbitMQ topology and a reliable publisher.

Topology (declared idempotently by every service that connects):

    simulator --publish--> [transit.events]  (topic exchange, durable)
                                 |  binding "#"
                                 v
                      (transit.events.process)  durable queue, competing consumers
                                 |  rejected / poison messages
                                 v
                      [transit.events.dlx] -> (transit.events.dead)

Guarantees provided here:
  * durable exchange + queue, persistent messages (delivery_mode=2) -> survive broker restart
  * publisher confirms -> publish() returns only after the broker has the message
  * message_id = event_id -> consumers can deduplicate
  * automatic reconnect with backoff if the broker is restarting

Consuming (manual ack after DB commit, prefetch, dedup) is the worker's job.
"""

from __future__ import annotations

import logging
import time

import pika
from pika.adapters.blocking_connection import BlockingChannel
from pika.exceptions import AMQPError

from transit.common.events import (
    DEAD_LETTER_EXCHANGE,
    DEAD_LETTER_QUEUE,
    EVENTS_QUEUE,
    EXCHANGE,
    BaseEvent,
    serialize,
)

log = logging.getLogger(__name__)


def connect(url: str, retries: int = 30, delay: float = 2.0) -> pika.BlockingConnection:
    """Open a blocking connection, retrying while the broker starts up."""
    params = pika.URLParameters(url)
    params.heartbeat = 60
    params.blocked_connection_timeout = 300
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return pika.BlockingConnection(params)
        except AMQPError as exc:
            last_error = exc
            log.warning("RabbitMQ not reachable (attempt %d/%d): %s", attempt, retries, exc)
            time.sleep(delay)
    raise ConnectionError(f"could not connect to RabbitMQ at {url}") from last_error


def declare_topology(channel: BlockingChannel) -> None:
    """Create exchanges and queues if missing. Safe to call from every service."""
    channel.exchange_declare(DEAD_LETTER_EXCHANGE, exchange_type="fanout", durable=True)
    channel.queue_declare(DEAD_LETTER_QUEUE, durable=True)
    channel.queue_bind(DEAD_LETTER_QUEUE, DEAD_LETTER_EXCHANGE)

    channel.exchange_declare(EXCHANGE, exchange_type="topic", durable=True)
    channel.queue_declare(
        EVENTS_QUEUE,
        durable=True,
        arguments={"x-dead-letter-exchange": DEAD_LETTER_EXCHANGE},
    )
    channel.queue_bind(EVENTS_QUEUE, EXCHANGE, routing_key="#")


class ReliablePublisher:
    """Publishes events with confirms; reconnects and retries on broker failure.

    publish() blocks until the broker confirms. If RabbitMQ is down, it keeps
    retrying, which pauses the simulation rather than dropping events.
    """

    def __init__(self, url: str):
        self.url = url
        self._connection: pika.BlockingConnection | None = None
        self._channel: BlockingChannel | None = None
        self.published = 0

    def _ensure_channel(self) -> BlockingChannel:
        if self._channel is None or self._channel.is_closed:
            self._connection = connect(self.url)
            self._channel = self._connection.channel()
            declare_topology(self._channel)
            self._channel.confirm_delivery()
        return self._channel

    def publish(self, event: BaseEvent) -> None:
        body = serialize(event)
        props = pika.BasicProperties(
            content_type="application/json",
            delivery_mode=pika.DeliveryMode.Persistent,
            message_id=str(event.event_id),
            type=event.routing_key,
        )
        backoff = 1.0
        while True:
            try:
                channel = self._ensure_channel()
                channel.basic_publish(EXCHANGE, event.routing_key, body, properties=props, mandatory=True)
                self.published += 1
                return
            except (AMQPError, ConnectionError, OSError) as exc:
                log.warning("publish failed (%s); reconnecting in %.0fs", exc, backoff)
                self._channel = None
                time.sleep(backoff)
                backoff = min(backoff * 2, 15)

    def process_events(self) -> None:
        """Service heartbeats while the simulator sleeps between events."""
        if self._connection is not None and self._connection.is_open:
            try:
                self._connection.process_data_events(time_limit=0)
            except AMQPError:
                self._channel = None

    def close(self) -> None:
        if self._connection is not None and self._connection.is_open:
            self._connection.close()
