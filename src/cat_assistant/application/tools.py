from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass

from cat_assistant.domain.models import ToolCall, ToolExecutionContext, ToolResult, ToolSpec
from cat_assistant.domain.ports import ToolPort


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    tool: ToolPort
    owner: str


class ToolRegistry:
    """Explicit capability registry with plugin ownership and safe projections."""

    def __init__(self, tools: Sequence[ToolPort] = ()) -> None:
        self._tools: dict[str, RegisteredTool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: ToolPort, *, owner: str = "core") -> None:
        if tool.spec.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.spec.name}")
        self._tools[tool.spec.name] = RegisteredTool(tool, owner)

    def unregister_owner(self, owner: str) -> tuple[str, ...]:
        removed = tuple(name for name, entry in self._tools.items() if entry.owner == owner)
        for name in removed:
            del self._tools[name]
        return removed

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(entry.tool.spec for entry in self._tools.values())

    @property
    def read_only_specs(self) -> tuple[ToolSpec, ...]:
        """The only projection exposed to the read-only diagnostic runner."""
        return tuple(
            entry.tool.spec
            for entry in self._tools.values()
            if entry.tool.spec.read_only
        )

    def owner_of(self, name: str) -> str | None:
        entry = self._tools.get(name)
        return entry.owner if entry else None

    async def execute(
        self,
        call: ToolCall,
        context: ToolExecutionContext,
        *,
        timeout_seconds: float = 30.0,
    ) -> ToolResult:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        entry = self._tools.get(call.name)
        if entry is None:
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                content=f"tool not allowed: {call.name}",
                is_error=True,
            )

        if not entry.tool.spec.read_only:
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                content=f"tool requires a control workflow: {call.name}",
                is_error=True,
            )

        try:
            content = await asyncio.wait_for(
                entry.tool.execute(call.arguments, context),
                timeout=timeout_seconds,
            )
            return ToolResult(call.call_id, call.name, content)
        except asyncio.TimeoutError:
            return ToolResult(
                call.call_id,
                call.name,
                f"tool timed out after {timeout_seconds:g}s: {call.name}",
                True,
            )
        except Exception as exc:  # tool failures are data for the model, not loop crashes
            return ToolResult(call.call_id, call.name, f"tool failed: {exc}", True)
