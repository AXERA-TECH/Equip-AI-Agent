"""Dependency-free session and scoped memory stores.

The JSONL implementations are intentionally small edge prototypes. They keep
conversation history and explicit memory separate from the append-only audit
event log so either store can later be replaced by SQLite or a service.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from cat_assistant.domain.models import MemoryHit, MemoryRecord, TurnRecord


class InMemorySessionStore:
    def __init__(self) -> None:
        self._turns: dict[str, list[TurnRecord]] = {}
        self._summaries: dict[str, str] = {}

    async def append_turn(self, turn: TurnRecord) -> None:
        self._turns.setdefault(turn.session_id, []).append(turn)

    async def recent_turns(self, session_id: str, *, limit: int = 8) -> tuple[TurnRecord, ...]:
        if limit < 1:
            return ()
        return tuple(self._turns.get(session_id, ())[-limit:])

    async def get_summary(self, session_id: str) -> str | None:
        return self._summaries.get(session_id)

    async def set_summary(self, session_id: str, summary: str) -> None:
        self._summaries[session_id] = summary.strip()

    async def delete_session(self, session_id: str) -> int:
        """Drop a session's turns and summary; returns removed turn count."""
        removed = len(self._turns.pop(session_id, ()))
        self._summaries.pop(session_id, None)
        return removed


class JsonlSessionStore:
    """Small persistent turn store; summaries live beside the turn log."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._summary_path = path.with_suffix(path.suffix + ".summaries.json")
        self._lock = asyncio.Lock()

    async def append_turn(self, turn: TurnRecord) -> None:
        record = asdict(turn)
        record["occurred_at"] = turn.occurred_at.isoformat()
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        async with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")

    async def recent_turns(self, session_id: str, *, limit: int = 8) -> tuple[TurnRecord, ...]:
        if limit < 1 or not self._path.exists():
            return ()
        records: list[TurnRecord] = []
        try:
            with self._path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        payload = json.loads(line)
                        if payload.get("session_id") != session_id:
                            continue
                        payload["occurred_at"] = datetime.fromisoformat(payload["occurred_at"])
                        records.append(TurnRecord(**payload))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
        except OSError:
            return ()
        return tuple(records[-limit:])

    async def get_summary(self, session_id: str) -> str | None:
        try:
            payload = json.loads(self._summary_path.read_text(encoding="utf-8"))
            value = payload.get(session_id)
            return value if isinstance(value, str) else None
        except (OSError, TypeError, json.JSONDecodeError):
            return None

    async def set_summary(self, session_id: str, summary: str) -> None:
        async with self._lock:
            payload: dict[str, str] = {}
            try:
                raw = json.loads(self._summary_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    payload = {str(key): str(value) for key, value in raw.items()}
            except (OSError, TypeError, json.JSONDecodeError):
                pass
            payload[session_id] = summary.strip()
            self._summary_path.parent.mkdir(parents=True, exist_ok=True)
            self._summary_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    async def delete_session(self, session_id: str) -> int:
        """Remove a session's turns and summary from disk.

        The turn log is rewritten atomically via a temp file so a crash cannot
        leave a half-written store. Lines that belong to other sessions and any
        unparseable lines are preserved; only turns for ``session_id`` are
        dropped. The summary entry is removed inline because this method already
        holds ``self._lock`` (``set_summary`` would deadlock if called here).
        """
        removed = 0
        async with self._lock:
            if self._path.exists():
                kept: list[str] = []
                try:
                    with self._path.open("r", encoding="utf-8") as stream:
                        for line in stream:
                            stripped = line.strip()
                            if not stripped:
                                continue
                            try:
                                payload = json.loads(stripped)
                            except json.JSONDecodeError:
                                kept.append(stripped)  # keep lines we cannot parse
                                continue
                            if isinstance(payload, dict) and payload.get("session_id") == session_id:
                                removed += 1
                                continue
                            kept.append(stripped)
                except OSError:
                    return 0
                temp_path = self._path.with_suffix(self._path.suffix + ".tmp")
                temp_path.write_text(
                    "".join(f"{item}\n" for item in kept),
                    encoding="utf-8",
                )
                temp_path.replace(self._path)
            self._drop_summary_locked(session_id)
        return removed

    def _drop_summary_locked(self, session_id: str) -> None:
        try:
            raw = json.loads(self._summary_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, json.JSONDecodeError):
            return
        if isinstance(raw, dict) and session_id in raw:
            payload = {str(key): str(value) for key, value in raw.items() if key != session_id}
            self._summary_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )


class InMemoryMemoryStore:
    def __init__(self, memories: tuple[MemoryRecord, ...] = ()) -> None:
        self.memories = list(memories)

    async def append(self, memory: MemoryRecord) -> None:
        self.memories.append(memory)

    async def search(
        self,
        query: str,
        *,
        machine_id: str | None = None,
        operator_id: str | None = None,
        limit: int = 5,
    ) -> tuple[MemoryHit, ...]:
        query_terms = {term for term in query.casefold().split() if term}
        ranked: list[MemoryHit] = []
        for memory in self.memories:
            if memory.machine_id not in (None, machine_id):
                continue
            if memory.operator_id not in (None, operator_id):
                continue
            content_terms = set(memory.content.casefold().split())
            overlap = len(query_terms & content_terms)
            if query_terms and overlap == 0:
                continue
            score = (overlap / len(query_terms)) if query_terms else 0.0
            ranked.append(MemoryHit(memory.content, memory.kind, memory.source, score, memory.memory_id))
        ranked.sort(key=lambda hit: (-hit.score, hit.memory_id))
        return tuple(ranked[: max(0, limit)])


class JsonlMemoryStore(InMemoryMemoryStore):
    """JSONL-backed explicit memory with the same simple lexical retrieval."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = path
        self._lock = asyncio.Lock()
        self._loaded = False

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._path.exists():
            return
        try:
            with self._path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        payload = json.loads(line)
                        payload["created_at"] = datetime.fromisoformat(payload["created_at"])
                        self.memories.append(MemoryRecord(**payload))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
        except OSError:
            return

    async def append(self, memory: MemoryRecord) -> None:
        await self._ensure_loaded()
        async with self._lock:
            self.memories.append(memory)
            record = asdict(memory)
            record["created_at"] = memory.created_at.isoformat()
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

    async def search(self, query: str, **kwargs: object) -> tuple[MemoryHit, ...]:
        await self._ensure_loaded()
        return await super().search(query, **kwargs)  # type: ignore[arg-type]
