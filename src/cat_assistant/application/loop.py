from __future__ import annotations

import asyncio
import inspect

from cat_assistant.adapters.memory import InMemoryMemoryStore, InMemorySessionStore
from cat_assistant.application.events import EventRecorder
from cat_assistant.application.orchestration import TaskOrchestrator
from cat_assistant.application.tracing import NoOpTracer, bind, unbind
from cat_assistant.domain.models import (
    AssistantResponse,
    ResponseCategory,
    MemoryRecord,
    TurnRecord,
    Utterance,
)
from cat_assistant.domain.ports import (
    MachineTelemetryPort,
    MemoryPort,
    SessionStorePort,
    Tracer,
)


class AgentLoop:
    """The outer loop: session, context, routing, persistence, and delivery boundary."""

    def __init__(
        self,
        telemetry: MachineTelemetryPort,
        orchestrator: TaskOrchestrator,
        events: EventRecorder,
        session_store: SessionStorePort | None = None,
        memory: MemoryPort | None = None,
        turn_timeout_seconds: float = 120.0,
        tracer: Tracer | None = None,
    ) -> None:
        if turn_timeout_seconds <= 0:
            raise ValueError("turn_timeout_seconds must be positive")
        self._telemetry = telemetry
        self._orchestrator = orchestrator
        self._events = events
        self._session_store = session_store or InMemorySessionStore()
        self._memory = memory or InMemoryMemoryStore()
        self._turn_timeout_seconds = turn_timeout_seconds
        self._tracer = tracer or NoOpTracer()
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._runtime_started = False
        self.plugin_manager = None
        self.runner = None
        self.tool_registry = None
        self.capability_registry = None

    async def shutdown(self) -> None:
        """Close plugin-owned resources when the host is stopping."""
        first_error: BaseException | None = None
        if self.plugin_manager is not None:
            try:
                await self.plugin_manager.shutdown()
            except BaseException as exc:
                first_error = exc
        if self.capability_registry is not None:
            try:
                await self.capability_registry.shutdown_all()
            except BaseException as exc:
                first_error = first_error or exc
        try:
            await self._tracer.flush()
        except Exception:
            pass
        self._runtime_started = False
        if first_error is not None:
            raise first_error

    async def startup(self) -> None:
        """Start all registered capabilities exactly once."""
        if self._runtime_started:
            return
        if self.capability_registry is not None:
            await self.capability_registry.start_all()
        self._runtime_started = True

    async def remember(self, memory: MemoryRecord) -> None:
        """Persist an explicitly validated memory item.

        The host deliberately exposes no automatic "LLM write memory" path;
        callers must decide what is safe and provide machine/operator scope.
        """
        if not memory.content.strip():
            raise ValueError("memory content must not be empty")
        if not 0.0 <= memory.confidence <= 1.0:
            raise ValueError("memory confidence must be between 0 and 1")
        await self._memory.append(memory)

    async def set_session_summary(self, session_id: str, summary: str) -> None:
        """Store a host-produced summary for later bounded context building."""
        await self._session_store.set_summary(session_id, summary)

    async def clear_session(self, session_id: str) -> int:
        """Delete a session's conversation history and summary.

        Explicit memory is intentionally left untouched: it is a separately
        scoped, deliberately persisted store, not conversation history. The
        deletion itself is recorded as an append-only audit event because the
        event log is independent of the session store. Returns the number of
        removed turns.
        """
        if not session_id.strip():
            raise ValueError("session_id must not be empty")
        removed = await self._session_store.delete_session(session_id)
        await self._events.record(
            "session/cleared",
            session_id,
            removed_turns=removed,
        )
        return removed

    async def get_machine_snapshot(self, machine_id: str):
        """Read the current machine snapshot for adapters such as the debug UI."""
        return await self._telemetry.get_snapshot(machine_id)

    async def list_machines(self):
        """Enumerate known machines for a host device picker, if telemetry supports it."""
        lister = getattr(self._telemetry, "list_snapshots", None)
        if lister is None:
            return ()
        snapshots = lister()
        if inspect.isawaitable(snapshots):
            snapshots = await snapshots
        return tuple(snapshots)

    async def get_recent_turns(
        self,
        session_id: str,
        *,
        machine_id: str | None = None,
        operator_id: str | None = None,
        limit: int = 20,
    ) -> tuple[TurnRecord, ...]:
        """Return scoped conversation history for trusted host interfaces."""
        if limit < 1:
            return ()
        turns = await self._session_store.recent_turns(session_id, limit=limit)
        return tuple(
            turn
            for turn in turns
            if (machine_id is None or turn.machine_id == machine_id)
            and (
                (operator_id is None and turn.operator_id is None)
                or (
                    operator_id is not None
                    and turn.operator_id in (None, operator_id)
                )
            )
        )

    @property
    def tool_specs(self):
        return self.tool_registry.specs if self.tool_registry is not None else ()

    @property
    def model_adapter_name(self) -> str:
        return self.runner.model_adapter_name if self.runner is not None else "unknown"

    @property
    def capability_specs(self):
        registry = self.capability_registry
        return registry.descriptors if registry is not None else ()

    @property
    def capability_inventory(self):
        registry = self.capability_registry
        return registry.inventory if registry is not None else ()

    async def handle(self, utterance: Utterance) -> AssistantResponse:
        lock = self._session_locks.setdefault(utterance.session_id, asyncio.Lock())
        async with lock:
            try:
                return await asyncio.wait_for(
                    self._run_turn(utterance),
                    timeout=self._turn_timeout_seconds,
                )
            except asyncio.TimeoutError:
                await self._events.record(
                    "turn/timeout",
                    utterance.session_id,
                    timeout_seconds=self._turn_timeout_seconds,
                )
                return AssistantResponse(
                    "本轮处理超过安全时间限制，已停止等待，请稍后重试。",
                    ResponseCategory.ERROR,
                    metadata={
                        "reason": "turn_timeout",
                        "timeout_seconds": self._turn_timeout_seconds,
                    },
                )

    async def _run_turn(self, utterance: Utterance) -> AssistantResponse:
        await self.startup()
        return await self._handle_turn(utterance)

    async def _handle_turn(self, utterance: Utterance) -> AssistantResponse:
        await self._events.record(
            "turn/started",
            utterance.session_id,
            machine_id=utterance.machine_id,
            operator_id=utterance.operator_id,
        )
        await self._events.record(
            "user/utterance",
            utterance.session_id,
            text=utterance.text,
            confidence=utterance.confidence,
        )

        trace = self._tracer.start_trace(
            "turn",
            session_id=utterance.session_id,
            user_id=utterance.operator_id,
            input=utterance.text,
            metadata={"machine_id": utterance.machine_id},
        )
        token = bind(trace)
        response: AssistantResponse | None = None
        try:
            machine = await self._telemetry.get_snapshot(utterance.machine_id)
            response = await self._orchestrator.handle(utterance, machine)

            await self._events.record(
                "assistant/response",
                utterance.session_id,
                text=response.text,
                category=response.category.value,
                requires_confirmation=response.requires_confirmation,
            )
            try:
                await self._session_store.append_turn(
                    TurnRecord(
                        session_id=utterance.session_id,
                        machine_id=utterance.machine_id,
                        user_text=utterance.text,
                        assistant_text=response.text,
                        category=response.category.value,
                        operator_id=utterance.operator_id,
                    )
                )
            except Exception as exc:
                # A completed safe reply should not become an error only because
                # the optional conversation-history store is unavailable.
                await self._events.record(
                    "session/save_failed",
                    utterance.session_id,
                    error_type=type(exc).__name__,
                )
            await self._events.record("turn/completed", utterance.session_id)
            return response
        except asyncio.CancelledError:
            await self._events.record("turn/cancelled", utterance.session_id)
            raise
        except Exception as exc:
            await self._events.record(
                "turn/failed",
                utterance.session_id,
                error_type=type(exc).__name__,
            )
            response = AssistantResponse(
                "当前无法安全完成请求，请稍后重试或联系维修人员。",
                ResponseCategory.ERROR,
            )
            return response
        finally:
            unbind(token)
            try:
                trace.end(
                    output=response.text if response is not None else None,
                    level=None if response is None or response.category is not ResponseCategory.ERROR else "ERROR",
                )
            except Exception:
                pass
