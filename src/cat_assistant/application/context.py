"""Build bounded, provenance-aware model context for one turn."""

from __future__ import annotations

from collections.abc import Sequence

from cat_assistant.domain.models import ChatMessage, MachineSnapshot, MemoryHit
from cat_assistant.domain.ports import MemoryPort, SessionStorePort


class ContextBuilder:
    """Compose current facts, short-term history and scoped memory.

    Device facts are always fetched by ``AgentLoop`` and passed in here. Memory
    is explicitly labelled as non-authoritative so it cannot replace telemetry.
    The character budget is a dependency-free approximation of a token budget.
    """

    def __init__(
        self,
        sessions: SessionStorePort,
        memory: MemoryPort,
        *,
        history_limit: int = 8,
        max_chars: int = 12_000,
    ) -> None:
        if history_limit < 0 or max_chars < 1:
            raise ValueError("history_limit must be non-negative and max_chars must be positive")
        self._sessions = sessions
        self._memory = memory
        self._history_limit = history_limit
        self._max_chars = max_chars

    async def build(
        self,
        *,
        session_id: str,
        user_text: str,
        machine: MachineSnapshot,
        operator_id: str | None = None,
    ) -> tuple[ChatMessage, ...]:
        try:
            history = await self._sessions.recent_turns(
                session_id,
                limit=self._history_limit,
            )
        except Exception:
            history = ()
        history = tuple(
            turn
            for turn in history
            if turn.machine_id == machine.machine_id
            and (operator_id is None or turn.operator_id in (None, operator_id))
        )
        try:
            summary = await self._sessions.get_summary(session_id)
        except Exception:
            summary = None
        try:
            memories = await self._memory.search(
                user_text,
                machine_id=machine.machine_id,
                operator_id=operator_id,
                limit=5,
            )
        except Exception:
            memories = ()

        messages: list[ChatMessage] = [
            ChatMessage(
                role="system",
                content=(
                    "You are a read-only equipment diagnostic assistant. "
                    "Use current telemetry as authoritative, treat memory as a hint, "
                    "cite document sources, and never claim to have changed or "
                    "controlled the machine."
                ),
            ),
            ChatMessage(
                role="system",
                content=(
                    f"Current machine (authoritative): model={machine.model}; "
                    f"serial={machine.serial_number}; state={machine.operating_state}; "
                    f"faults={list(machine.fault_codes)}; captured_at={machine.captured_at.isoformat()}"
                ),
            ),
        ]
        if summary:
            messages.append(
                ChatMessage(
                    role="system",
                    content=f"Conversation summary (non-authoritative): {summary}",
                )
            )
        if memories:
            messages.append(ChatMessage(role="system", content=self._format_memories(memories)))

        for turn in history:
            messages.append(ChatMessage(role="user", content=turn.user_text))
            messages.append(ChatMessage(role="assistant", content=turn.assistant_text))
        messages.append(ChatMessage(role="user", content=user_text))
        return self._fit_budget(messages)

    @staticmethod
    def _format_memories(memories: Sequence[MemoryHit]) -> str:
        lines = [
            "Scoped memory (not authoritative device state; verify before relying on it):"
        ]
        lines.extend(
            f"- [{memory.kind}/{memory.source}] {memory.content} (score={memory.score:.2f})"
            for memory in memories
        )
        return "\n".join(lines)

    def _fit_budget(self, messages: list[ChatMessage]) -> tuple[ChatMessage, ...]:
        total = sum(len(message.content) for message in messages)
        if total <= self._max_chars:
            return tuple(messages)

        # Preserve system policy/current facts and the current request. Drop the
        # oldest conversational pairs first, then trim memory/summary if needed.
        protected = messages[:2]
        tail = messages[2:]
        while tail and sum(len(item.content) for item in protected + tail) > self._max_chars:
            if len(tail) >= 2 and tail[0].role == "user" and tail[1].role == "assistant":
                tail = tail[2:]
            else:
                tail = tail[1:]
        if sum(len(item.content) for item in protected + tail) <= self._max_chars:
            return tuple(protected + tail)

        current = messages[-1]
        available = max(1, self._max_chars - sum(len(item.content) for item in protected) - 1)
        return tuple(protected + [ChatMessage(role=current.role, content=current.content[:available])])
