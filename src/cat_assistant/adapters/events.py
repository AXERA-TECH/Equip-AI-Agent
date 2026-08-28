from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from cat_assistant.domain.models import DomainEvent


class InMemoryEventStore:
    def __init__(self) -> None:
        self.events: list[DomainEvent] = []

    async def append(self, event: DomainEvent) -> None:
        self.events.append(event)


class JsonlEventStore:
    """Small append-only audit log suitable for the initial edge prototype."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    async def append(self, event: DomainEvent) -> None:
        record = asdict(event)
        record["occurred_at"] = event.occurred_at.isoformat()
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        async with self._lock:
            # A prototype event is one short line. Keep the dependency-free
            # implementation synchronous; production deployments should replace
            # this adapter with a dedicated non-blocking persistence service.
            self._append_line(line)

    def _append_line(self, line: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as stream:
            stream.write(line)
            stream.write("\n")
