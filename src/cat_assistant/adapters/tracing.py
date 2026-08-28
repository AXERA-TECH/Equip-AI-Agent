"""Tracing adapters: an in-memory recorder, a Langfuse client, and a model
decorator that reports each LLM call as a generation on the current trace node.

The concrete external adapter (:class:`LangfuseTracer`) lazily imports the
optional ``langfuse`` package and degrades to no-ops on any error, so tracing is
never able to break a diagnostic turn.
"""

from __future__ import annotations

import asyncio
from typing import Any

from cat_assistant.application.tracing import NO_OP_NODE, current_node
from cat_assistant.domain.models import ChatMessage, ModelReply, ToolSpec
from cat_assistant.domain.ports import LanguageModelPort, TraceNode


# --------------------------------------------------------------------------- #
# In-memory recorder (offline inspection and tests)
# --------------------------------------------------------------------------- #
class RecordedNode:
    """A recorded trace/span/generation node with its children."""

    def __init__(
        self,
        *,
        type: str,
        name: str,
        model: str | None = None,
        input: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.type = type
        self.name = name
        self.model = model
        self.input = input
        self.metadata = metadata
        self.output: Any = None
        self.level: str | None = None
        self.status_message: str | None = None
        self.usage: dict[str, Any] | None = None
        self.ended = False
        self.children: list[RecordedNode] = []

    def span(
        self, name: str, *, input: Any = None, metadata: dict[str, Any] | None = None
    ) -> "RecordedNode":
        child = RecordedNode(type="span", name=name, input=input, metadata=metadata)
        self.children.append(child)
        return child

    def generation(
        self,
        name: str,
        *,
        model: str,
        input: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> "RecordedNode":
        child = RecordedNode(
            type="generation", name=name, model=model, input=input, metadata=metadata
        )
        self.children.append(child)
        return child

    def end(
        self,
        *,
        output: Any = None,
        level: str | None = None,
        status_message: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        self.output = output
        self.level = level
        self.status_message = status_message
        self.usage = usage
        self.ended = True

    def iter_descendants(self) -> "list[RecordedNode]":
        collected: list[RecordedNode] = []
        for child in self.children:
            collected.append(child)
            collected.extend(child.iter_descendants())
        return collected

    def descendant_names(self) -> list[str]:
        return [node.name for node in self.iter_descendants()]


class RecordingTracer:
    """Keeps the full trace tree in memory. Useful for tests and local debugging."""

    def __init__(self) -> None:
        self.traces: list[RecordedNode] = []

    def start_trace(
        self,
        name: str,
        *,
        session_id: str,
        user_id: str | None = None,
        input: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> RecordedNode:
        node = RecordedNode(
            type="trace",
            name=name,
            input=input,
            metadata={"session_id": session_id, "user_id": user_id, **(metadata or {})},
        )
        self.traces.append(node)
        return node

    async def flush(self) -> None:
        return None


# --------------------------------------------------------------------------- #
# Langfuse adapter (optional dependency)
# --------------------------------------------------------------------------- #
class _LangfuseNode:
    """Wraps a Langfuse observation (span/generation), mapping our port onto the
    v4 OpenTelemetry-style SDK API (``start_observation`` / ``update`` / ``end``),
    with a fallback to the v3 ``start_span`` / ``start_generation`` methods.
    """

    def __init__(self, obj: Any) -> None:
        self._obj = obj

    def span(
        self, name: str, *, input: Any = None, metadata: dict[str, Any] | None = None
    ) -> TraceNode:
        try:
            return _LangfuseNode(
                _start_child(self._obj, name=name, as_type="span", input=input, metadata=metadata)
            )
        except Exception:
            return NO_OP_NODE

    def generation(
        self,
        name: str,
        *,
        model: str,
        input: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> TraceNode:
        try:
            return _LangfuseNode(
                _start_child(
                    self._obj,
                    name=name,
                    as_type="generation",
                    model=model,
                    input=input,
                    metadata=metadata,
                )
            )
        except Exception:
            return NO_OP_NODE

    def end(
        self,
        *,
        output: Any = None,
        level: str | None = None,
        status_message: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        try:
            update_kwargs: dict[str, Any] = {}
            if output is not None:
                update_kwargs["output"] = output
            if level is not None:
                update_kwargs["level"] = level  # v4 accepts DEBUG/DEFAULT/WARNING/ERROR
            if status_message is not None:
                update_kwargs["status_message"] = status_message
            if usage is not None:
                update_kwargs["usage_details"] = usage
            if update_kwargs:
                self._obj.update(**update_kwargs)
            self._obj.end()
        except Exception:
            pass


def _start_child(
    parent: Any,
    *,
    name: str,
    as_type: str,
    model: str | None = None,
    input: Any = None,
    metadata: dict[str, Any] | None = None,
) -> Any:
    """Create a child observation, preferring v4's ``start_observation``."""
    starter = getattr(parent, "start_observation", None)
    if starter is not None:
        kwargs: dict[str, Any] = {"name": name, "as_type": as_type, "input": input, "metadata": metadata}
        if as_type == "generation" and model is not None:
            kwargs["model"] = model
        return starter(**kwargs)
    # Fallback for langfuse v3, which exposes start_span / start_generation.
    if as_type == "generation":
        return parent.start_generation(name=name, model=model, input=input, metadata=metadata)
    return parent.start_span(name=name, input=input, metadata=metadata)


class LangfuseTracer:
    """Emits traces/spans/generations to Langfuse via its official SDK (v3/v4).

    Credentials default to the standard ``LANGFUSE_PUBLIC_KEY`` /
    ``LANGFUSE_SECRET_KEY`` / ``LANGFUSE_HOST`` environment variables that the
    SDK itself reads, so secrets never pass through local config files. A turn is
    the root observation; ``session_id`` / ``user_id`` are recorded in its
    metadata (visible on the trace in the Langfuse UI).
    """

    def __init__(
        self,
        *,
        public_key: str | None = None,
        secret_key: str | None = None,
        host: str | None = None,
    ) -> None:
        try:
            from langfuse import Langfuse
        except ImportError as exc:  # pragma: no cover - exercised only when unset
            raise RuntimeError(
                "Langfuse tracing requires the optional 'langfuse' package. "
                "Install it with: pip install 'equip-ai-agent[tracing]'."
            ) from exc
        kwargs: dict[str, Any] = {}
        if public_key:
            kwargs["public_key"] = public_key
        if secret_key:
            kwargs["secret_key"] = secret_key
        if host:
            kwargs["host"] = host
        self._client = Langfuse(**kwargs)

    def start_trace(
        self,
        name: str,
        *,
        session_id: str,
        user_id: str | None = None,
        input: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> TraceNode:
        try:
            root_metadata = {"session_id": session_id, "user_id": user_id, **(metadata or {})}
            root = _start_child(
                self._client, name=name, as_type="span", input=input, metadata=root_metadata
            )
            return _LangfuseNode(root)
        except Exception:
            return NO_OP_NODE

    async def flush(self) -> None:
        try:
            await asyncio.to_thread(self._client.flush)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Model decorator: one generation per LLM call, nested under the current node
# --------------------------------------------------------------------------- #
class TracedLanguageModel:
    """Wrap any LanguageModelPort and report each ``complete`` as a generation.

    The generation attaches to whatever trace node is current (the plan.build
    span for planner calls, a step span for the diagnostic agent), so the tree
    reflects where each call happened. With no active trace it simply delegates.
    """

    def __init__(self, wrapped: LanguageModelPort) -> None:
        self.wrapped = wrapped

    async def complete(
        self,
        messages: "list[ChatMessage] | tuple[ChatMessage, ...]",
        tools: "list[ToolSpec] | tuple[ToolSpec, ...]",
    ) -> ModelReply:
        parent = current_node()
        if parent is None:
            return await self.wrapped.complete(messages, tools)
        model_name = str(getattr(self.wrapped, "model", type(self.wrapped).__name__))
        tool_specs = tuple(tools)
        metadata: dict[str, Any] = {"tool_count": len(tool_specs)}
        if tool_specs:
            # Tools ride a separate top-level ``tools`` request parameter, not the
            # message list, so they never appear in ``input``. Record the exact
            # name/description/schema the model was offered here — otherwise the
            # trace cannot explain why the planner selected (or under-selected)
            # the tools it did.
            metadata["tools"] = [_tool_view(tool) for tool in tool_specs]
        generation = parent.generation(
            "llm.generation",
            model=model_name,
            input=[_message_view(message) for message in messages],
            metadata=metadata,
        )
        try:
            reply = await self.wrapped.complete(messages, tools)
        except BaseException as exc:
            _safe_end(
                generation,
                level="ERROR",
                status_message=f"{type(exc).__name__}: {exc}",
            )
            raise
        _safe_end(
            generation,
            output=_reply_view(reply),
            usage=_usage_details(reply.usage),
        )
        return reply


def _message_view(message: ChatMessage) -> dict[str, Any]:
    view: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.name:
        view["name"] = message.name
    if message.tool_calls:
        view["tool_calls"] = [
            {"name": call.name, "arguments": call.arguments}
            for call in message.tool_calls
        ]
    return view


def _tool_view(tool: ToolSpec) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.input_schema,
    }


def _reply_view(reply: ModelReply) -> dict[str, Any]:
    view: dict[str, Any] = {
        "content": reply.content,
        "tool_calls": [
            {"name": call.name, "arguments": call.arguments}
            for call in reply.tool_calls
        ],
    }
    # Only present on real adapter replies; makes "deliberate stop vs truncated"
    # ("tool_calls"/"stop" vs "length") readable straight off the generation node.
    if reply.finish_reason is not None:
        view["finish_reason"] = reply.finish_reason
    return view


def _usage_details(usage: dict[str, int] | None) -> dict[str, int] | None:
    """Map our normalized token counts onto Langfuse ``usage_details`` keys.

    Langfuse aggregates the standard ``input``/``output``/``total`` keys for its
    token and cost dashboards; ``reasoning`` is carried through as an extra
    detail so a reasoning model's thinking budget sits beside its visible-output
    tokens on the trace. Returns None when the reply had no usage.
    """
    if not usage:
        return None
    mapping = {
        "prompt_tokens": "input",
        "completion_tokens": "output",
        "total_tokens": "total",
        "reasoning_tokens": "reasoning",
    }
    details = {
        target: usage[source] for source, target in mapping.items() if source in usage
    }
    return details or None


def _safe_end(node: TraceNode, **kwargs: Any) -> None:
    try:
        node.end(**kwargs)
    except Exception:
        pass
