from __future__ import annotations

import asyncio
import hashlib
import inspect
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from cat_assistant.domain.capabilities import (
    Artifact,
    CapabilityDescriptor,
    CapabilityResult,
)
from cat_assistant.domain.models import MachineSnapshot, ToolSpec, Utterance
from cat_assistant.domain.plans import TaskStep


@dataclass(slots=True)
class CapabilityExecutionContext:
    utterance: Utterance
    machine: MachineSnapshot
    artifacts: dict[str, tuple[Artifact, ...]] = field(default_factory=dict)

    def dependency_artifacts(self, step: TaskStep) -> tuple[Artifact, ...]:
        return tuple(
            artifact
            for dependency in step.depends_on
            for artifact in self.artifacts.get(dependency, ())
        )

    def artifacts_of_type(self, artifact_type: str) -> tuple[Artifact, ...]:
        return tuple(
            artifact
            for artifacts in self.artifacts.values()
            for artifact in artifacts
            if artifact.artifact_type == artifact_type
        )


class CapabilityProvider(Protocol):
    @property
    def descriptor(self) -> CapabilityDescriptor: ...

    async def execute(
        self,
        arguments: dict[str, Any],
        context: CapabilityExecutionContext,
        step: TaskStep,
    ) -> CapabilityResult: ...


@dataclass(slots=True)
class RegisteredCapability:
    provider: CapabilityProvider
    owner: str
    enabled: bool = True
    started: bool = False
    lifecycle_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class CapabilityRegistry:
    """Provider registry with ownership, lifecycle, discovery and validation."""

    def __init__(self) -> None:
        self._entries: dict[str, RegisteredCapability] = {}

    def register(
        self,
        provider: CapabilityProvider,
        *,
        owner: str = "core",
        enabled: bool = True,
    ) -> None:
        descriptor = provider.descriptor
        if descriptor.name in self._entries:
            raise ValueError(f"duplicate capability: {descriptor.name}")
        if descriptor.timeout_seconds < 1:
            raise ValueError("capability timeout_seconds must be positive")
        self._entries[descriptor.name] = RegisteredCapability(provider, owner, enabled)

    def unregister_owner(self, owner: str) -> tuple[str, ...]:
        """Remove entries after their async lifecycle has been closed."""
        if any(
            entry.owner == owner and entry.started
            for entry in self._entries.values()
        ):
            raise RuntimeError(
                f"cannot unregister started capabilities for {owner}; "
                "call shutdown_owner() first"
            )
        removed = tuple(
            name for name, entry in self._entries.items() if entry.owner == owner
        )
        for name in removed:
            del self._entries[name]
        return removed

    def descriptor(self, name: str) -> CapabilityDescriptor | None:
        entry = self._entries.get(name)
        return entry.provider.descriptor if entry else None

    def provider(self, name: str) -> CapabilityProvider | None:
        entry = self._entries.get(name)
        return entry.provider if entry and entry.enabled else None

    def owner_of(self, name: str) -> str | None:
        entry = self._entries.get(name)
        return entry.owner if entry else None

    def is_enabled(self, name: str) -> bool:
        entry = self._entries.get(name)
        return bool(entry and entry.enabled)

    @property
    def descriptors(self) -> tuple[CapabilityDescriptor, ...]:
        return tuple(entry.provider.descriptor for entry in self._entries.values())

    @property
    def enabled_descriptors(self) -> tuple[CapabilityDescriptor, ...]:
        return tuple(
            entry.provider.descriptor
            for entry in self._entries.values()
            if entry.enabled
        )

    @property
    def model_tools(self) -> tuple[ToolSpec, ...]:
        """Project every model-selectable enabled capability into an LLM tool.

        Capabilities flagged ``model_selectable=False`` stay enabled and
        executable (the rule planner and dependency graph still use them) but are
        withheld from the model's menu, keeping the small edge model's tool list
        short and purpose-distinct.
        """
        return tuple(
            ToolSpec(
                name=capability_tool_name(descriptor.name),
                description=_model_tool_description(descriptor),
                input_schema=descriptor.input_schema,
                read_only=descriptor.side_effect == "none",
                capability=descriptor.name,
            )
            for descriptor in self.enabled_descriptors
            if descriptor.model_selectable
        )

    def capability_for_model_tool(self, tool_name: str) -> str | None:
        for descriptor in self.enabled_descriptors:
            if capability_tool_name(descriptor.name) == tool_name:
                return descriptor.name
        return None

    def validate_arguments(self, name: str, arguments: dict[str, Any]) -> None:
        entry = self._require(name)
        if not entry.enabled:
            raise ValueError("capability is not enabled")
        _validate_object(arguments, entry.provider.descriptor.input_schema)

    @property
    def inventory(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "name": entry.provider.descriptor.name,
                "version": entry.provider.descriptor.version,
                "description": entry.provider.descriptor.description,
                "kind": entry.provider.descriptor.kind,
                "risk_level": entry.provider.descriptor.risk_level,
                "side_effect": entry.provider.descriptor.side_effect,
                "requires_approval": entry.provider.descriptor.requires_approval,
                "source": entry.provider.descriptor.source,
                "owner": entry.owner,
                "enabled": entry.enabled,
                "consumes": entry.provider.descriptor.consumes,
                "produces": entry.provider.descriptor.produces,
            }
            for entry in self._entries.values()
        )

    def discover(
        self,
        text: str,
        *,
        kind: str | None = None,
    ) -> tuple[CapabilityDescriptor, ...]:
        normalized = text.casefold()
        return tuple(
            descriptor
            for descriptor in self.enabled_descriptors
            if descriptor.auto_select
            and (kind is None or descriptor.kind == kind)
            and descriptor.trigger_terms
            and any(term.casefold() in normalized for term in descriptor.trigger_terms)
        )

    async def enable(self, name: str) -> None:
        entry = self._require(name)
        if not entry.enabled:
            entry.enabled = True
        await self._start_entry(entry)

    async def disable(self, name: str) -> None:
        entry = self._require(name)
        async with entry.lifecycle_lock:
            if entry.started:
                await _call_lifecycle(entry.provider, "shutdown")
                entry.started = False
            entry.enabled = False

    async def start_all(self) -> None:
        """Start every enabled Provider exactly once."""
        started: list[RegisteredCapability] = []
        try:
            for entry in tuple(self._entries.values()):
                if entry.enabled and not entry.started:
                    await self._start_entry(entry)
                    started.append(entry)
        except BaseException:
            for entry in reversed(started):
                await self._stop_entry(entry)
            raise

    async def shutdown_owner(self, owner: str) -> None:
        """Stop all started Providers owned by a plugin before removal."""
        first_error: BaseException | None = None
        for entry in tuple(self._entries.values()):
            if entry.owner == owner:
                try:
                    await self._stop_entry(entry)
                except BaseException as exc:
                    first_error = first_error or exc
        if first_error is not None:
            raise first_error

    async def shutdown_all(self) -> None:
        """Stop all started Providers; safe to call repeatedly."""
        first_error: BaseException | None = None
        for entry in tuple(self._entries.values()):
            try:
                await self._stop_entry(entry)
            except BaseException as exc:
                first_error = first_error or exc
        if first_error is not None:
            raise first_error

    async def health(self, name: str) -> dict[str, Any]:
        entry = self._require(name)
        callback = getattr(entry.provider, "health", None)
        if callback is None:
            return {"ok": entry.enabled, "status": "enabled" if entry.enabled else "disabled"}
        value = callback()
        if inspect.isawaitable(value):
            value = await value
        if not isinstance(value, dict):
            raise TypeError("capability health() must return a dict")
        return value

    async def execute(
        self,
        step: TaskStep,
        context: CapabilityExecutionContext,
    ) -> CapabilityResult:
        entry = self._require(step.capability)
        if not entry.enabled:
            return CapabilityResult(status="failed", content="能力当前未启用。")
        await self._start_entry(entry)
        descriptor = entry.provider.descriptor
        if descriptor.supported_machine_models and context.machine.model not in descriptor.supported_machine_models:
            return CapabilityResult(status="failed", content="能力不支持当前设备型号。")
        _validate_object(step.arguments, descriptor.input_schema)
        result = await asyncio.wait_for(
            entry.provider.execute(step.arguments, context, step),
            timeout=descriptor.timeout_seconds,
        )
        if not isinstance(result, CapabilityResult):
            raise TypeError("capability provider must return CapabilityResult")
        for artifact in result.artifacts:
            if artifact.source_capability != descriptor.name:
                raise ValueError("artifact source_capability does not match provider")
            _validate_value(artifact.data, descriptor.output_schema, "capability output")
        return result

    def _require(self, name: str) -> RegisteredCapability:
        try:
            return self._entries[name]
        except KeyError as exc:
            raise LookupError(f"capability is not registered: {name}") from exc

    async def _start_entry(self, entry: RegisteredCapability) -> None:
        async with entry.lifecycle_lock:
            if entry.started:
                return
            await _call_lifecycle(entry.provider, "startup")
            entry.started = True

    async def _stop_entry(self, entry: RegisteredCapability) -> None:
        async with entry.lifecycle_lock:
            if not entry.started:
                return
            await _call_lifecycle(entry.provider, "shutdown")
            entry.started = False


async def _call_lifecycle(provider: CapabilityProvider, method: str) -> None:
    callback = getattr(provider, method, None)
    if callback is None:
        return
    value = callback()
    if inspect.isawaitable(value):
        await value


def _validate_object(value: dict[str, Any], schema: dict[str, Any]) -> None:
    """Validate the small JSON Schema subset used by local capability contracts."""

    if schema.get("type", "object") != "object":
        raise ValueError("capability input schema root must be object")
    required = schema.get("required", ())
    missing = [name for name in required if name not in value]
    if missing:
        raise ValueError(f"missing capability arguments: {', '.join(missing)}")
    properties = schema.get("properties", {})
    if schema.get("additionalProperties") is False:
        unknown = set(value) - set(properties)
        if unknown:
            raise ValueError(f"unknown capability arguments: {', '.join(sorted(unknown))}")
    for name, item in value.items():
        expected = properties.get(name, {}).get("type")
        if expected == "string" and not isinstance(item, str):
            raise ValueError(f"capability argument {name} must be a string")
        if expected == "number" and not isinstance(item, (int, float)):
            raise ValueError(f"capability argument {name} must be a number")
        if expected == "boolean" and not isinstance(item, bool):
            raise ValueError(f"capability argument {name} must be a boolean")


def _validate_value(value: Any, schema: dict[str, Any], label: str) -> None:
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            raise ValueError(f"{label} must be an object")
        required = schema.get("required", ())
        missing = [name for name in required if name not in value]
        if missing:
            raise ValueError(f"{label} is missing: {', '.join(missing)}")
    elif expected == "array" and not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be an array")
    elif expected == "string" and not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    elif expected == "number" and not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    elif expected == "boolean" and not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")


def capability_tool_name(capability_name: str) -> str:
    """Return a readable, collision-resistant OpenAI function name."""
    readable = re.sub(r"[^a-zA-Z0-9_-]+", "_", capability_name).strip("_")
    readable = readable[:45] or "capability"
    digest = hashlib.sha256(capability_name.encode("utf-8")).hexdigest()[:8]
    return f"cap_{readable}_{digest}"


def _model_tool_description(descriptor: CapabilityDescriptor) -> str:
    """面向模型的中文工具说明。

    只保留“做什么”和“是否改变设备状态/是否需要审批”这类与选择直接相关的信息。
    consumes/produces 是内部工件名，对小参数模型是噪声——工件依赖已由执行器的
    依赖图自动串联（每个后续步骤都会拿到之前步骤的全部产物），无需模型操心，
    因此不再暴露，以缩短提示、减少跨语言干扰。
    """
    if descriptor.requires_approval or descriptor.side_effect != "none":
        gate = "会改变设备状态，需操作员审批"
    else:
        gate = "只读，不改变设备状态"
    return f"{descriptor.description}（{gate}）"
