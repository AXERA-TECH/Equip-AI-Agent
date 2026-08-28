"""Turn-scoped tracing glue: a context variable holding the current trace node.

The concrete tracer (Langfuse or a recording double) is bound by the outer
loop; everything in between creates child spans via :func:`span`, and the model
decorator attaches generations to whatever node is current. Tracing is strictly
best-effort — a `None` current node (no active trace) makes every call a cheap
no-op, and node methods never raise into the caller.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any, Iterator

from cat_assistant.domain.ports import TraceNode


_CURRENT: ContextVar[TraceNode | None] = ContextVar("cat_trace_node", default=None)


def current_node() -> TraceNode | None:
    """Return the node new observations should attach to, or None if untraced."""
    return _CURRENT.get()


def bind(node: TraceNode) -> Token:
    """Make ``node`` the current trace node; returns a token to unbind with."""
    return _CURRENT.set(node)


def unbind(token: Token) -> None:
    try:
        _CURRENT.reset(token)
    except (ValueError, LookupError):
        # The token belongs to another context (e.g. reset after task handoff);
        # tracing state is advisory, so ignore rather than fail the turn.
        pass


class SpanScope:
    """Mutable handle yielded by :func:`span` so callers can set an outcome."""

    __slots__ = ("output", "level", "status_message")

    def __init__(self) -> None:
        self.output: Any = None
        self.level: str | None = None
        self.status_message: str | None = None


@contextmanager
def span(
    name: str,
    *,
    input: Any = None,
    metadata: dict[str, Any] | None = None,
) -> Iterator[SpanScope]:
    """Open a child span of the current node for the duration of the block.

    When no trace is active this is a cheap no-op that still yields a scope, so
    call sites need no conditional. Set ``scope.output`` to record the span's
    result; exceptions are recorded as an ERROR level and re-raised.
    """
    parent = _CURRENT.get()
    scope = SpanScope()
    if parent is None:
        yield scope
        return
    node = parent.span(name, input=input, metadata=metadata)
    token = _CURRENT.set(node)
    try:
        yield scope
    except BaseException as exc:
        if scope.level is None:
            scope.level = "ERROR"
            scope.status_message = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        unbind(token)
        try:
            node.end(
                output=scope.output,
                level=scope.level,
                status_message=scope.status_message,
            )
        except Exception:
            pass


class _NoOpNode:
    """A trace node that records nothing. Shared singleton; safe to reuse."""

    def span(
        self, name: str, *, input: Any = None, metadata: dict[str, Any] | None = None
    ) -> "_NoOpNode":
        return self

    def generation(
        self,
        name: str,
        *,
        model: str,
        input: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> "_NoOpNode":
        return self

    def end(
        self,
        *,
        output: Any = None,
        level: str | None = None,
        status_message: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        return None


NO_OP_NODE = _NoOpNode()


class NoOpTracer:
    """Default tracer: produces no telemetry. Used when Langfuse is unconfigured."""

    def start_trace(
        self,
        name: str,
        *,
        session_id: str,
        user_id: str | None = None,
        input: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> _NoOpNode:
        return NO_OP_NODE

    async def flush(self) -> None:
        return None
