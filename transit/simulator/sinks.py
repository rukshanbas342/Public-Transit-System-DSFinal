"""Where simulator events go. The engine only knows the EventSink interface.

    rabbitmq  production path: durable, confirmed publishing (see common/messaging.py)
    jsonl     one JSON event per line in a file - inspect or replay without a broker
    memory    keep events in a list - unit tests and headless experiments
    none      discard - fastest, for metrics-only runs
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from transit.common.events import BaseEvent, serialize


class EventSink(Protocol):
    def publish(self, event: BaseEvent) -> None: ...
    def tick(self) -> None: ...
    def close(self) -> None: ...


class NullSink:
    def __init__(self) -> None:
        self.count = 0

    def publish(self, event: BaseEvent) -> None:
        self.count += 1

    def tick(self) -> None:
        pass

    def close(self) -> None:
        pass


class MemorySink(NullSink):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[BaseEvent] = []

    def publish(self, event: BaseEvent) -> None:
        super().publish(event)
        self.events.append(event)

    def of_type(self, event_type: str) -> list[BaseEvent]:
        return [e for e in self.events if e.event_type == event_type]  # type: ignore[attr-defined]


class JsonlSink(NullSink):
    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "w")

    def publish(self, event: BaseEvent) -> None:
        super().publish(event)
        self._fh.write(serialize(event).decode() + "\n")

    def close(self) -> None:
        self._fh.close()


class RabbitSink:
    def __init__(self, url: str) -> None:
        from transit.common.messaging import ReliablePublisher

        self._publisher = ReliablePublisher(url)

    @property
    def count(self) -> int:
        return self._publisher.published

    def publish(self, event: BaseEvent) -> None:
        self._publisher.publish(event)

    def tick(self) -> None:
        self._publisher.process_events()

    def close(self) -> None:
        self._publisher.close()


def make_sink(kind: str, *, rabbitmq_url: str | None = None, jsonl_path: str | None = None) -> EventSink:
    if kind == "rabbitmq":
        if not rabbitmq_url:
            raise ValueError("rabbitmq sink needs RABBITMQ_URL")
        return RabbitSink(rabbitmq_url)
    if kind == "jsonl":
        return JsonlSink(jsonl_path or "out/events.jsonl")
    if kind == "memory":
        return MemorySink()
    if kind == "none":
        return NullSink()
    raise ValueError(f"unknown sink {kind!r}")
