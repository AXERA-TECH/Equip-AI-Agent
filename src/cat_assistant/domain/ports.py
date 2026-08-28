from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from cat_assistant.domain.models import (
    ChatMessage,
    ControlOutcome,
    DomainEvent,
    KnowledgeHit,
    MemoryHit,
    MemoryRecord,
    MachineSnapshot,
    ModelReply,
    ToolExecutionContext,
    ToolSpec,
    TurnRecord,
)


class MachineTelemetryPort(Protocol):
    async def get_snapshot(self, machine_id: str) -> MachineSnapshot: ...


class MachineControlPort(Protocol):
    """A safety-gated actuator for machine-state changes.

    Implementations run only after the deterministic PolicyEngine and operator
    approval. The offline demo binds this to a simulated executor; production
    deployments would bind a real, audited Safety Gateway client.
    """

    async def apply(self, request: str, *, machine: MachineSnapshot) -> ControlOutcome: ...


class KnowledgePort(Protocol):
    async def search(
        self,
        query: str,
        *,
        machine: MachineSnapshot,
        limit: int = 3,
    ) -> Sequence[KnowledgeHit]: ...


class SessionStorePort(Protocol):
    async def append_turn(self, turn: TurnRecord) -> None: ...

    async def recent_turns(self, session_id: str, *, limit: int = 8) -> Sequence[TurnRecord]: ...

    async def get_summary(self, session_id: str) -> str | None: ...

    async def set_summary(self, session_id: str, summary: str) -> None: ...

    async def delete_session(self, session_id: str) -> int: ...


class MemoryPort(Protocol):
    async def search(
        self,
        query: str,
        *,
        machine_id: str | None = None,
        operator_id: str | None = None,
        limit: int = 5,
    ) -> Sequence[MemoryHit]: ...

    async def append(self, memory: MemoryRecord) -> None: ...


class LanguageModelPort(Protocol):
    async def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
    ) -> ModelReply: ...


class EventStorePort(Protocol):
    async def append(self, event: DomainEvent) -> None: ...


class ToolPort(Protocol):
    @property
    def spec(self) -> ToolSpec: ...

    async def execute(
        self,
        arguments: dict[str, object],
        context: ToolExecutionContext,
    ) -> str: ...


class SpeechRecognizerPort(Protocol):
    async def transcribe(self, audio: bytes) -> tuple[str, float]: ...


class SpeechSynthesizerPort(Protocol):
    async def synthesize(self, text: str) -> bytes: ...


class TraceNode(Protocol):
    """A trace, span or generation that can nest child observations.

    The API is intentionally provider-agnostic and best-effort: every method
    must be safe to call and must never raise into the caller, so observability
    can never break a diagnostic turn.
    """

    def span(
        self,
        name: str,
        *,
        input: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> "TraceNode": ...

    def generation(
        self,
        name: str,
        *,
        model: str,
        input: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> "TraceNode": ...

    def end(
        self,
        *,
        output: Any = None,
        level: str | None = None,
        status_message: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None: ...


class Tracer(Protocol):
    """Starts per-turn traces. Bind an adapter (Langfuse) or the no-op default."""

    def start_trace(
        self,
        name: str,
        *,
        session_id: str,
        user_id: str | None = None,
        input: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> TraceNode: ...

    async def flush(self) -> None: ...
