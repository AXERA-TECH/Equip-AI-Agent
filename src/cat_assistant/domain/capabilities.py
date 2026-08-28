from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class Artifact:
    """A typed, provenance-aware product passed between task steps."""

    artifact_type: str
    data: Any
    source_capability: str
    confidence: float = 1.0
    provenance: dict[str, Any] = field(default_factory=dict)
    artifact_id: str = field(default_factory=lambda: str(uuid4()))
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    """Stable contract advertised by built-in, plugin, MCP or Skill providers."""

    name: str
    version: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    kind: str = "observe"
    risk_level: str = "low"
    side_effect: str = "none"
    requires_approval: bool = False
    source: str = "builtin"
    timeout_seconds: int = 30
    supported_machine_models: tuple[str, ...] = ()
    trigger_terms: tuple[str, ...] = ()
    consumes: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    auto_select: bool = False
    visible_in_response: bool = False
    # Whether the LLM planner may pick this capability. Plumbing steps (an
    # authoritative telemetry read whose data is already on the execution
    # context), deterministic rule-planner-only answers, and niche auto_select
    # observers stay registered and executable but are hidden from the model's
    # tool menu, so a small edge model faces a shorter, purpose-distinct list.
    model_selectable: bool = True


@dataclass(frozen=True, slots=True)
class CapabilityResult:
    status: str = "completed"
    content: str = ""
    artifacts: tuple[Artifact, ...] = ()
    response_category: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
