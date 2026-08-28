from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from cat_assistant.domain.capabilities import Artifact


@dataclass(frozen=True, slots=True)
class TaskStep:
    """One node in a request plan. Steps form a small dependency DAG."""

    step_id: str
    capability: str
    arguments: dict[str, Any] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TaskPlan:
    """Structured plan produced before any capability is executed."""

    plan_id: str = field(default_factory=lambda: str(uuid4()))
    steps: tuple[TaskStep, ...] = ()
    reason: str = ""


@dataclass(frozen=True, slots=True)
class StepResult:
    step_id: str
    capability: str
    status: str
    content: str = ""
    artifacts: tuple[Artifact, ...] = ()
    response_category: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
