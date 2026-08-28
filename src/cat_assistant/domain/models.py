from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


class ResponseCategory(str, Enum):
    INFORMATION = "information"
    DIAGNOSTIC = "diagnostic"
    CLARIFICATION = "clarification"
    ERROR = "error"
    APPROVAL_REQUIRED = "approval_required"


@dataclass(frozen=True, slots=True)
class Utterance:
    text: str
    session_id: str
    machine_id: str
    operator_id: str | None = None
    confidence: float = 1.0


@dataclass(frozen=True, slots=True)
class MachineSnapshot:
    machine_id: str
    model: str
    serial_number: str
    engine_running: bool
    operating_state: str
    hour_meter: float
    next_service_hours: float
    fuel_percent: float
    fault_codes: tuple[str, ...] = ()
    captured_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class ControlOutcome:
    """Result of a machine-control actuation reported by a MachineControlPort.

    ``machine`` is the resulting snapshot when the actuation changed device
    state (so the caller can persist it as the new authoritative reading), or
    ``None`` when the command was accepted without a concrete state change.
    """

    accepted: bool
    summary: str
    machine: MachineSnapshot | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeHit:
    content: str
    source: str
    score: float
    document_version: str


@dataclass(frozen=True, slots=True)
class TurnRecord:
    """Persisted conversational turn used for short-term context."""

    session_id: str
    machine_id: str
    user_text: str
    assistant_text: str
    category: str
    operator_id: str | None = None
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """An explicit, scoped memory item; it is never authoritative device state."""

    content: str
    kind: str = "fact"
    source: str = "operator"
    machine_id: str | None = None
    operator_id: str | None = None
    confidence: float = 1.0
    memory_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class MemoryHit:
    content: str
    kind: str
    source: str
    score: float
    memory_id: str


@dataclass(frozen=True, slots=True)
class AssistantResponse:
    text: str
    category: ResponseCategory
    requires_confirmation: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    # Read-only tools may be exposed to the diagnostic AgentRunner. Write-capable
    # tools require a separate control workflow and are never shown by default.
    read_only: bool = True
    capability: str = "diagnostic"


@dataclass(frozen=True, slots=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]
    call_id: str = field(default_factory=lambda: str(uuid4()))


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: str
    content: str = ""
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelReply:
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    # Populated by real chat adapters; doubles/demo models leave them None.
    # ``finish_reason`` distinguishes a deliberate stop ("stop"/"tool_calls")
    # from truncation ("length"); ``usage`` carries token counts, including
    # ``reasoning_tokens`` for reasoning models (the figure that reveals a model
    # that spent its whole budget thinking before it could emit tool calls).
    finish_reason: str | None = None
    usage: dict[str, int] | None = None


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    session_id: str
    machine_id: str
    snapshot: MachineSnapshot


@dataclass(frozen=True, slots=True)
class DomainEvent:
    event_type: str
    session_id: str
    payload: dict[str, Any]
    event_id: str = field(default_factory=lambda: str(uuid4()))
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
