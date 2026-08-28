from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence

from cat_assistant.application.events import EventRecorder
from cat_assistant.application.context import ContextBuilder
from cat_assistant.application.tools import ToolRegistry
from cat_assistant.domain.models import (
    AssistantResponse,
    ChatMessage,
    MachineSnapshot,
    ResponseCategory,
    ToolExecutionContext,
)
from cat_assistant.domain.ports import LanguageModelPort


class BoundedAgentRunner:
    """The inner loop: one model step followed by zero or more tool executions."""

    def __init__(
        self,
        model: LanguageModelPort,
        tools: ToolRegistry,
        events: EventRecorder,
        *,
        max_steps: int = 4,
        context_builder: ContextBuilder | None = None,
        model_call_timeout_seconds: float = 60.0,
        tool_call_timeout_seconds: float = 30.0,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        if model_call_timeout_seconds <= 0 or tool_call_timeout_seconds <= 0:
            raise ValueError("model and tool timeouts must be positive")
        self._model = model
        self._tools = tools
        self._events = events
        self._max_steps = max_steps
        self._context_builder = context_builder
        self._model_call_timeout_seconds = model_call_timeout_seconds
        self._tool_call_timeout_seconds = tool_call_timeout_seconds

    @property
    def tool_specs(self):
        """Expose the runner's current tool projection to debug/admin UIs."""
        return self._tools.specs

    @property
    def model_adapter_name(self) -> str:
        """Stable adapter name for diagnostics and local configuration status."""
        model = getattr(self._model, "wrapped", self._model)
        return type(model).__name__

    async def run(
        self,
        *,
        session_id: str,
        user_text: str,
        machine: MachineSnapshot,
        operator_id: str | None = None,
        initial_messages: Sequence[ChatMessage] | None = None,
    ) -> AssistantResponse:
        if initial_messages is not None:
            messages = list(initial_messages)
        elif self._context_builder is None:
            messages = [
                ChatMessage(
                    role="system",
                    content=(
                        "You are a read-only equipment diagnostic assistant. "
                        "Use only the supplied tools, cite document sources, and never "
                        "claim to have changed or controlled the machine."
                    ),
                ),
                ChatMessage(
                    role="system",
                    content=(
                        f"Machine: {machine.model}; serial={machine.serial_number}; "
                        f"state={machine.operating_state}; faults={list(machine.fault_codes)}"
                    ),
                ),
                ChatMessage(role="user", content=user_text),
            ]
        else:
            messages = list(
                await self._context_builder.build(
                    session_id=session_id,
                    user_text=user_text,
                    machine=machine,
                    operator_id=operator_id,
                )
            )
        tool_context = ToolExecutionContext(session_id, machine.machine_id, machine)
        # Snapshot the opening prompt (system policy + telemetry + any injected
        # evidence + the user turn) before the loop appends tool exchanges. The
        # dead-end salvage below re-synthesizes from this clean context: replaying
        # the failed tool turns keeps a stubborn edge model in "call a tool" mode,
        # whereas the tool-free opening prompt reliably yields a grounded answer.
        base_messages = list(messages)

        for step in range(1, self._max_steps + 1):
            await self._events.record("step/started", session_id, step=step)
            try:
                reply = await asyncio.wait_for(
                    self._model.complete(messages, self._tools.read_only_specs),
                    timeout=self._model_call_timeout_seconds,
                )
            except asyncio.TimeoutError:
                await self._events.record(
                    "model/timeout",
                    session_id,
                    step=step,
                    timeout_seconds=self._model_call_timeout_seconds,
                )
                return AssistantResponse(
                    "模型响应超过安全时间限制，暂时无法形成可靠结论。",
                    ResponseCategory.ERROR,
                    metadata={
                        "reason": "model_timeout",
                        "timeout_seconds": self._model_call_timeout_seconds,
                    },
                )

            if not reply.tool_calls:
                text = reply.content.strip()
                if not text:
                    # Dead-end salvage: the model returned neither an answer nor a
                    # usable tool call — e.g. a small edge model emitted a malformed
                    # <tool_call> that could not be recovered. Re-synthesize from the
                    # clean opening prompt (telemetry and evidence are already there)
                    # with tools withheld, so the model writes a grounded answer
                    # instead of attempting (and re-botching) another tool call.
                    await self._events.record("model/empty_salvage", session_id, step=step)
                    try:
                        salvage = await asyncio.wait_for(
                            self._model.complete(base_messages, ()),
                            timeout=self._model_call_timeout_seconds,
                        )
                        text = salvage.content.strip()
                    except asyncio.TimeoutError:
                        text = ""
                if not text:
                    text = "我暂时无法根据本机资料得出可靠结论。"
                await self._events.record("step/completed", session_id, step=step)
                return AssistantResponse(text, ResponseCategory.DIAGNOSTIC)

            messages.append(
                ChatMessage(
                    role="assistant",
                    content=reply.content,
                    tool_calls=reply.tool_calls,
                )
            )

            for call in reply.tool_calls:
                await self._events.record(
                    "tool/called",
                    session_id,
                    step=step,
                    tool=call.name,
                    call_id=call.call_id,
                    arguments=call.arguments,
                )
                result = await self._tools.execute(
                    call,
                    tool_context,
                    timeout_seconds=self._tool_call_timeout_seconds,
                )
                await self._events.record(
                    "tool/result",
                    session_id,
                    step=step,
                    tool=result.name,
                    call_id=result.call_id,
                    is_error=result.is_error,
                )
                messages.append(
                    ChatMessage(
                        role="tool",
                        name=result.name,
                        tool_call_id=result.call_id,
                        content=json.dumps(
                            {"content": result.content, "is_error": result.is_error},
                            ensure_ascii=False,
                        ),
                    )
                )

            await self._events.record("step/completed", session_id, step=step)

        await self._events.record(
            "agent/limit_reached",
            session_id,
            max_steps=self._max_steps,
        )
        return AssistantResponse(
            "诊断步骤已达到本机安全限制，未形成可靠结论，请由维修技师继续检查。",
            ResponseCategory.ERROR,
            metadata={"reason": "max_steps"},
        )
