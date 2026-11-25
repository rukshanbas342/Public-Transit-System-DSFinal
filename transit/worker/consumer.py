"""Reliable RabbitMQ consumer for simulation events."""

from __future__ import annotations

import logging
import signal
import threading
from typing import Any

import pika
from pika.exceptions import AMQPError
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from transit.common.config import InfraSettings
from transit.common.db import session_scope
from transit.common.events import EVENTS_QUEUE, parse_event
from transit.common.messaging import connect, declare_topology
from transit.worker.handlers import process_event

log = logging.getLogger(__name__)


class Worker:
    def __init__(
        self,
        database_url: str,
        rabbitmq_url: str,
        *,
        prefetch_count: int = 10,
    ) -> None:
        self.database_url = database_url
        self.rabbitmq_url = rabbitmq_url
        self.prefetch_count = prefetch_count
        self.stop_event = threading.Event()
        self._connection: pika.BlockingConnection | None = None
        self._channel: pika.adapters.blocking_connection.BlockingChannel | None = None

    def stop(self, *_: Any) -> None:
        self.stop_event.set()
        connection = self._connection
        if connection is not None and connection.is_open:
            connection.add_callback_threadsafe(self._stop_consuming)

    def _stop_consuming(self) -> None:
        if self._channel is not None and self._channel.is_open:
            self._channel.stop_consuming()

    def _callback(self, channel: pika.adapters.blocking_connection.BlockingChannel, method, properties, body) -> None:
        try:
            event = parse_event(body)
        except (ValidationError, ValueError, TypeError) as exc:
            log.error("rejecting invalid event: %s", exc)
            channel.basic_reject(delivery_tag=method.delivery_tag, requeue=False)
            return

        try:
            with session_scope(self.database_url) as session:
                processed = process_event(session, event)
        except (SQLAlchemyError, OSError):
            log.exception("event processing failed; requeueing event %s", event.event_id)
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
            return
        except (LookupError, ValueError) as exc:
            log.error("rejecting unprocessable event %s: %s", event.event_id, exc)
            channel.basic_reject(delivery_tag=method.delivery_tag, requeue=False)
            return

        channel.basic_ack(delivery_tag=method.delivery_tag)
        if processed:
            log.debug("processed %s (%s)", event.event_id, event.event_type)
        else:
            log.debug("skipped duplicate %s (%s)", event.event_id, event.event_type)

    def run(self) -> None:
        while not self.stop_event.is_set():
            connection: pika.BlockingConnection | None = None
            try:
                connection = connect(self.rabbitmq_url)
                self._connection = connection
                channel = connection.channel()
                self._channel = channel
                declare_topology(channel)
                channel.basic_qos(prefetch_count=self.prefetch_count)
                channel.basic_consume(
                    queue=EVENTS_QUEUE,
                    on_message_callback=self._callback,
                    auto_ack=False,
                )
                log.info("worker consuming %s (prefetch=%d)", EVENTS_QUEUE, self.prefetch_count)
                channel.start_consuming()
            except (AMQPError, ConnectionError, OSError, RuntimeError) as exc:
                if not self.stop_event.is_set():
                    log.warning("consumer connection lost: %s; reconnecting", exc)
            finally:
                self._connection = None
                self._channel = None
                if connection is not None and connection.is_open:
                    connection.close()


def run_worker(
    database_url: str | None = None,
    rabbitmq_url: str | None = None,
    *,
    prefetch_count: int = 10,
) -> None:
    settings = InfraSettings.from_env()
    worker = Worker(
        database_url or settings.database_url,
        rabbitmq_url or settings.rabbitmq_url,
        prefetch_count=prefetch_count,
    )
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    worker.run()
