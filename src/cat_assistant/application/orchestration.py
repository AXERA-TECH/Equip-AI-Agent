"""Task planning, policy evaluation and dependency-graph execution."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Protocol

from cat_assistant.application.capabilities import (
    CapabilityExecutionContext,
    CapabilityRegistry,
)
from cat_assistant.application.events import EventRecorder
from cat_assistant.application import tracing
from cat_assistant.domain.capabilities import Artifact, CapabilityDescriptor
from cat_assistant.domain.models import (
    AssistantResponse,
    ChatMessage,
    MachineSnapshot,
    ModelReply,
    ResponseCategory,
    ToolSpec,
    Utterance,
)
from cat_assistant.domain.plans import StepResult, TaskPlan, TaskStep
from cat_assistant.domain.ports import LanguageModelPort


class TaskPlannerPort(Protocol):
    async def build(self, utterance: Utterance, machine: MachineSnapshot) -> TaskPlan: ...


class PolicyEngine:
    """Deterministic gate for capability risk and approval requirements."""

    def evaluate(
        self,
        descriptor: CapabilityDescriptor | None,
        *,
        approved: bool = False,
    ) -> str:
        if descriptor is None:
            return "denied: unknown capability"
        if descriptor.side_effect != "none" or descriptor.requires_approval:
            if not approved:
                return "approval_required"
        return "allow"


class RuleBasedTaskPlanner:
    """Offline planner with registry-driven observation capability discovery."""

    _knowledge_words = (
        "故障",
        "检查",
        "维修",
        "手册",
        "原因",
        "怎么",
        "解决",
        "查询",
        "manual",
        "diagnos",
        "e123",
    )
    _action_words = (
        "启动",
        "停止",
        "停机",
        "移动",
        "升高",
        "降低",
        "设置",
        "修改",
        "执行",
        "start",
        "stop",
        "set ",
    )
    _proposal_words = ("方案", "建议", "步骤", "如何处理", "怎么解决", "给出执行")

    def __init__(self, capabilities: CapabilityRegistry) -> None:
        self._capabilities = capabilities

    async def build(self, utterance: Utterance, machine: MachineSnapshot) -> TaskPlan:
        del machine
        text = utterance.text.strip()
        lowered = text.casefold()
        needs_knowledge = any(word in lowered for word in self._knowledge_words)
        explicit_action = any(word in lowered for word in self._action_words)
        # A read-only request must not accidentally enter the control path just
        # because it contains wording such as “不要执行任何操作”.
        if any(phrase in lowered for phrase in ("只读", "只读检查", "不要执行", "无需执行", "不执行任何操作")):
            explicit_action = False
        proposal_only = any(word in lowered for word in self._proposal_words)
        discovered = self._capabilities.discover(text)
        discovered_by_kind = {
            kind: tuple(item for item in discovered if item.kind == kind)
            for kind in ("observe", "retrieve", "infer", "propose", "act", "verify")
        }
        needs_diagnostic = needs_knowledge or bool(discovered) or (
            not self._is_simple_fact(lowered) and not explicit_action
        )

        steps: list[TaskStep] = [TaskStep("observe", "telemetry.read")]
        evidence_dependencies = ["observe"]
        for index, descriptor in enumerate(discovered_by_kind["observe"], start=1):
            step_id = f"observe-{index}"
            arguments = self._arguments_for(descriptor, text)
            steps.append(TaskStep(step_id, descriptor.name, arguments, ("observe",)))
            evidence_dependencies.append(step_id)

        # The current-state summary is a machine-relevant preamble, not a reply
        # owed to every message. Surface it only when the request genuinely
        # concerns the equipment (fault/knowledge wording or auto-discovered
        # observations) or precedes an action/proposal. Open-ended and
        # conversational turns still reach the model — which already receives the
        # authoritative snapshot in its context — but no longer get the status
        # line prepended, so unrelated questions stay clean.
        needs_summary = (
            needs_knowledge or bool(discovered) or explicit_action or proposal_only
        )
        if needs_summary:
            steps.append(TaskStep("state", "telemetry.summarize", {}, ("observe",)))
            evidence_dependencies.append("state")
        # ``knowledge.search`` can reach the plan two ways: a keyword hit
        # (needs_knowledge) or its own auto_select trigger_terms surfacing it
        # through discover(). Fold both into a single canonical "retrieve" step
        # so a fault-status question ("机器存在什么问题吗") still searches the
        # manuals, the node is never planned twice, and the debug UI keeps
        # labelling it 检索 (the label is keyed on this exact step id).
        knowledge_search_discovered = any(
            descriptor.name == "knowledge.search"
            for descriptor in discovered_by_kind["retrieve"]
        )
        if needs_knowledge or knowledge_search_discovered:
            steps.append(
                TaskStep(
                    "retrieve",
                    "knowledge.search",
                    {"query": text},
                    tuple(evidence_dependencies),
                )
            )
            evidence_dependencies.append("retrieve")
        for index, descriptor in enumerate(
            (
                descriptor
                for descriptor in discovered_by_kind["retrieve"]
                if descriptor.name != "knowledge.search"
            ),
            start=1,
        ):
            step_id = f"retrieve-dynamic-{index}"
            steps.append(
                TaskStep(
                    step_id,
                    descriptor.name,
                    self._arguments_for(descriptor, text),
                    tuple(evidence_dependencies),
                )
            )
            evidence_dependencies.append(step_id)
        for index, descriptor in enumerate(discovered_by_kind["infer"], start=1):
            step_id = f"infer-dynamic-{index}"
            steps.append(
                TaskStep(
                    step_id,
                    descriptor.name,
                    self._arguments_for(descriptor, text),
                    tuple(evidence_dependencies),
                )
            )
            evidence_dependencies.append(step_id)
        if needs_diagnostic:
            steps.append(
                TaskStep(
                    "diagnose",
                    "diagnostic.agent",
                    {},
                    tuple(evidence_dependencies),
                )
            )
            final_dependencies = ["diagnose"]
        elif not needs_knowledge and not needs_summary:
            steps.append(TaskStep("answer", "response.compose", {"text": text}, ("observe",)))
            final_dependencies = ["answer"]
        else:
            final_dependencies = list(evidence_dependencies)

        if proposal_only or explicit_action:
            steps.append(
                TaskStep(
                    "propose",
                    "action.propose",
                    {"request": text},
                    tuple(final_dependencies),
                )
            )
            final_dependencies = ["propose"]
        for index, descriptor in enumerate(discovered_by_kind["propose"], start=1):
            step_id = f"propose-dynamic-{index}"
            steps.append(
                TaskStep(
                    step_id,
                    descriptor.name,
                    self._arguments_for(descriptor, text),
                    tuple(final_dependencies),
                )
            )
            final_dependencies = [step_id]

        action_dependencies = list(final_dependencies)
        action_steps: list[str] = []
        for index, descriptor in enumerate(discovered_by_kind["act"], start=1):
            step_id = f"act-dynamic-{index}"
            steps.append(
                TaskStep(
                    step_id,
                    descriptor.name,
                    self._arguments_for(descriptor, text),
                    tuple(action_dependencies),
                )
            )
            action_steps.append(step_id)
            action_dependencies = [step_id]
        if explicit_action and not proposal_only and not action_steps:
            steps.append(
                TaskStep(
                    "act",
                    "machine.control",
                    {"request": text},
                    tuple(final_dependencies),
                )
            )
            action_steps.append("act")
        verify_dependencies = action_steps or final_dependencies
        for index, descriptor in enumerate(discovered_by_kind["verify"], start=1):
            step_id = f"verify-dynamic-{index}"
            steps.append(
                TaskStep(
                    step_id,
                    descriptor.name,
                    self._arguments_for(descriptor, text),
                    tuple(verify_dependencies),
                )
            )
            verify_dependencies = [step_id]
        return TaskPlan(steps=tuple(steps), reason="registry-driven request decomposition")

    @staticmethod
    def _arguments_for(descriptor: CapabilityDescriptor, text: str) -> dict[str, str]:
        properties = descriptor.input_schema.get("properties", {})
        if "text" in properties:
            return {"text": text}
        if "query" in properties:
            return {"query": text}
        if "request" in properties:
            return {"request": text}
        return {}

    @staticmethod
    def _is_simple_fact(text: str) -> bool:
        return any(
            word in text
            for word in ("状态", "油量", "燃油", "小时数", "保养", "status", "fuel", "service")
        )


def _is_deterministic_answer_plan(plan: TaskPlan) -> bool:
    """True when a plan is answered entirely by the deterministic response
    composer.

    ``response.compose`` is emitted only by the rule planner's simple
    status/fact branch, which is reached exactly when the turn needs no
    retrieval, diagnosis, proposal or actuation — so such a plan is just
    ``telemetry.read -> response.compose`` and nothing that would benefit from
    the model ever co-occurs with it. That makes the presence of a
    ``response.compose`` step a sound, cheap signal that an LLM planning pass
    would add nothing and can be skipped.
    """
    return any(step.capability == "response.compose" for step in plan.steps)


class ToolCallingTaskPlanner:
    """Let an LLM select from the complete enabled capability set."""

    def __init__(
        self,
        model: LanguageModelPort,
        capabilities: CapabilityRegistry,
        fallback: TaskPlannerPort,
        *,
        max_steps: int | None = None,
        model_call_timeout_seconds: float = 60.0,
        events: EventRecorder | None = None,
    ) -> None:
        if max_steps is not None and max_steps < 1:
            raise ValueError("max_steps must be positive, or None for no limit")
        if model_call_timeout_seconds <= 0:
            raise ValueError("model_call_timeout_seconds must be positive")
        self._model = model
        self._capabilities = capabilities
        self._fallback = fallback
        self._max_steps = max_steps
        self._model_call_timeout_seconds = model_call_timeout_seconds
        self._events = events

    async def build(self, utterance: Utterance, machine: MachineSnapshot) -> TaskPlan:
        tools = self._capabilities.model_tools
        if not tools:
            return await self._fallback.build(utterance, machine)
        # Fast path for bare fact/status questions. The rule planner is pure (no
        # model call), so run it first: when it would answer the turn with its
        # deterministic ``response.compose`` branch — e.g. "下次保养时间" or "现在
        # 什么状态" — an LLM planning pass adds nothing and would only be discarded
        # as an empty selection, so skip the slow (~10 tok/s) model call and use
        # the rule plan directly. Anything needing retrieval, diagnosis, a
        # proposal or an action does not reach that branch and still gets full
        # LLM planning below.
        fallback_plan = await self._fallback.build(utterance, machine)
        if _is_deterministic_answer_plan(fallback_plan):
            if self._events is not None:
                await self._events.record(
                    "planning/fast_path",
                    utterance.session_id,
                    reason="deterministic_fact",
                    steps=[step.capability for step in fallback_plan.steps],
                )
            return fallback_plan
        messages = (
            ChatMessage(role="system", content=self._planning_instructions(machine, tools)),
            ChatMessage(role="user", content=utterance.text),
        )
        try:
            reply = await asyncio.wait_for(
                self._model.complete(messages, tools),
                timeout=self._model_call_timeout_seconds,
            )
        except asyncio.TimeoutError:
            if self._events is not None:
                await self._events.record(
                    "planning/model_timeout",
                    utterance.session_id,
                    timeout_seconds=self._model_call_timeout_seconds,
                )
            return await self._fallback.build(utterance, machine)
        except Exception as exc:
            return await self._reject(
                utterance, machine, "model_error", error=type(exc).__name__
            )
        if not reply.tool_calls:
            return await self._reject(utterance, machine, "empty_selection", reply=reply)
        if self._max_steps is not None and len(reply.tool_calls) > self._max_steps:
            return await self._reject(
                utterance,
                machine,
                "too_many_calls",
                reply=reply,
                call_count=len(reply.tool_calls),
            )

        steps: list[TaskStep] = []
        for index, call in enumerate(reply.tool_calls, start=1):
            capability = self._capabilities.capability_for_model_tool(call.name)
            if capability is None:
                return await self._reject(
                    utterance, machine, "unknown_tool", reply=reply, tool=call.name
                )
            # Real models routinely misname a free-text argument (``text`` for a
            # tool whose slot is ``query``) or add an undeclared field. Reroute a
            # misnamed value onto the declared slot and strip forbidden keys, so a
            # fixable argument slip doesn't sink an otherwise-correct plan; a value
            # missing under every synonym still fails validation below.
            arguments, coercions = self._sanitize_arguments(capability, call.arguments)
            if coercions and self._events is not None:
                await self._events.record(
                    "planning/llm_args_coerced",
                    utterance.session_id,
                    tool=call.name,
                    capability=capability,
                    remapped=[{"from": src, "to": dst} for src, dst in coercions],
                )
            try:
                self._capabilities.validate_arguments(capability, arguments)
            except (LookupError, ValueError) as exc:
                return await self._reject(
                    utterance,
                    machine,
                    "invalid_arguments",
                    reply=reply,
                    tool=call.name,
                    capability=capability,
                    error=str(exc),
                )
            # Every later step receives all earlier artifacts. This intentionally
            # serializes the small-capability route and keeps evidence complete.
            dependencies = tuple(step.step_id for step in steps)
            steps.append(
                TaskStep(
                    step_id=f"model-{index}",
                    capability=capability,
                    arguments=arguments,
                    depends_on=dependencies,
                )
            )
        # A plan that only gathers evidence (telemetry read / knowledge search)
        # but selects no capability that speaks to the operator would execute
        # successfully yet yield the empty "任务已完成，但没有可展示的结果" reply.
        # Rather than discard the model's (correct) evidence selection, append a
        # terminal answer-producing capability that runs last and consumes the
        # gathered evidence, so the operator still gets a diagnosis. Only when no
        # such capability exists do we fall back to the deterministic planner,
        # which always ends with a user-facing step.
        if not any(
            (descriptor := self._capabilities.descriptor(step.capability)) is not None
            and descriptor.visible_in_response
            for step in steps
        ):
            terminal = self._terminal_answer_capability()
            if terminal is None:
                return await self._reject(
                    utterance,
                    machine,
                    "no_visible_answer",
                    reply=reply,
                    capabilities=[step.capability for step in steps],
                )
            if self._events is not None:
                await self._events.record(
                    "planning/llm_plan_repaired",
                    utterance.session_id,
                    reason="appended_terminal_answer",
                    appended=terminal,
                    selected_tools=[step.capability for step in steps],
                )
            steps.append(
                TaskStep(
                    step_id=f"model-{len(steps) + 1}",
                    capability=terminal,
                    arguments={},
                    depends_on=tuple(step.step_id for step in steps),
                )
            )
            return TaskPlan(
                steps=tuple(steps),
                reason="llm tool-call capability selection (terminal answer appended)",
            )
        return TaskPlan(
            steps=tuple(steps),
            reason="llm tool-call capability selection",
        )

    async def _reject(
        self,
        utterance: Utterance,
        machine: MachineSnapshot,
        reason: str,
        *,
        reply: ModelReply | None = None,
        **details: object,
    ) -> TaskPlan:
        """Record why the LLM plan was discarded, then fall back to rules.

        Every LLM-plan rejection used to fall back silently, so the events log
        could not distinguish "LLM planned it" from "LLM plan thrown away" — only
        ``plan.reason`` hinted at it. This makes each rejection observable with a
        machine-readable ``reason`` (empty_selection, unknown_tool,
        invalid_arguments, no_visible_answer, too_many_calls, model_error).

        When the model actually replied (every reason except a raw
        ``model_error`` or timeout), its ``finish_reason`` and token ``usage`` are
        attached too, so the log alone separates truncation
        (``finish_reason="length"``) from a model that deliberately stopped after
        an inadequate selection (``"tool_calls"``/``"stop"``) — no trace dive.
        ``selected_tools`` records the model's *whole* capability selection (not
        just the one that failed), so a plan rejected at its first bad tool still
        shows the full intent behind it.
        """
        if self._events is not None:
            payload = dict(details)
            if reply is not None:
                if reply.finish_reason is not None:
                    payload["finish_reason"] = reply.finish_reason
                if reply.usage:
                    payload["usage"] = reply.usage
                if reply.tool_calls:
                    payload["selected_tools"] = [
                        self._capabilities.capability_for_model_tool(call.name)
                        or call.name
                        for call in reply.tool_calls
                    ]
            await self._events.record(
                "planning/llm_plan_rejected",
                utterance.session_id,
                reason=reason,
                **payload,
            )
        return await self._fallback.build(utterance, machine)

    # Free-text argument keys that all mean "the user's text/intent". Real models
    # routinely pick a plausible-but-wrong one (e.g. ``text`` for a tool whose
    # parameter is ``query`` or ``request``), so a value under any of these is a
    # candidate to reroute onto the slot a schema actually declares.
    _ARGUMENT_SYNONYMS = (
        "text", "query", "request", "input", "prompt", "question", "content", "message", "q",
    )

    def _sanitize_arguments(
        self, capability: str, arguments: dict[str, object]
    ) -> tuple[dict[str, object], list[tuple[str, str]]]:
        """Reroute misnamed free-text keys onto the declared slot, then drop
        undeclared keys.

        Two real-model failure modes are handled: (1) a value placed under a
        wrong-but-synonymous key — ``{"text": ...}`` for a tool whose sole
        required string parameter is ``query`` — is rerouted to that parameter
        rather than discarded; (2) an extra hallucinated field under
        ``additionalProperties: false`` is stripped so it can't sink an
        otherwise-correct plan. A required field with no value under any synonym
        still fails validation. Returns the cleaned arguments plus the list of
        ``(from, to)`` remaps applied, so the caller can record the coercion.
        """
        descriptor = self._capabilities.descriptor(capability)
        if descriptor is None:
            return dict(arguments), []
        schema = descriptor.input_schema
        properties = schema.get("properties") or {}
        required = schema.get("required") or ()
        result = dict(arguments)
        coercions: list[tuple[str, str]] = []
        consumed: set[str] = set()
        for prop in required:
            if prop in result:
                continue
            if (properties.get(prop) or {}).get("type") != "string":
                continue  # only reroute free-text slots, never invent structure
            for synonym in self._ARGUMENT_SYNONYMS:
                if synonym == prop or synonym in consumed:
                    continue
                value = result.get(synonym)
                if isinstance(value, str) and value:
                    result[prop] = value
                    coercions.append((synonym, prop))
                    consumed.add(synonym)
                    break
        if schema.get("additionalProperties") is False:
            allowed = set(properties)
            result = {key: value for key, value in result.items() if key in allowed}
        return result, coercions


    def _terminal_answer_capability(self) -> str | None:
        """Choose a terminal capability to append when the model gathered
        evidence but selected nothing the operator can see.

        The candidate must be safe to append unconditionally: enabled,
        operator-visible, free of side effects and approval gates, and callable
        with empty arguments (so we never have to synthesize a value it
        requires). Among those, prefer an inference/diagnosis step over a bare
        summary, and one that consumes more evidence types — which selects
        ``diagnostic.agent`` (it reasons over the telemetry snapshot and any
        manual hits) ahead of ``telemetry.summarize``. Returns None when the
        registry offers no such capability, leaving the caller to fall back to
        the deterministic planner.
        """
        candidates = [
            descriptor
            for descriptor in self._capabilities.enabled_descriptors
            if descriptor.visible_in_response
            and descriptor.side_effect == "none"
            and not descriptor.requires_approval
            and not (descriptor.input_schema.get("required") or ())
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda descriptor: (descriptor.kind == "infer", len(descriptor.consumes)),
            reverse=True,
        )
        return candidates[0].name

    def _planning_instructions(
        self, machine: MachineSnapshot, tools: Sequence[ToolSpec]
    ) -> str:
        # 列出当前真正可选的“产出答复”工具名（与模型收到的哈希名一致），让
        # “必须以出答复的工具收尾”这条规则指向具体选项，而非抽象类别。
        answer_tools = [
            spec.name
            for spec in tools
            if (descriptor := self._capabilities.descriptor(spec.capability)) is not None
            and descriptor.visible_in_response
        ]
        answer_list = "、".join(answer_tools) if answer_tools else "（当前无可用项）"
        budget = (
            f"最多使用 {self._max_steps} 次工具调用；在此限度内，宁可完整也不要遗漏。\n"
            if self._max_steps is not None
            else ""
        )
        return (
            "你是工业设备助手的任务规划器。你不直接回答用户，而是在这一次回复里"
            "一次性、按执行顺序选出所有需要的能力工具（通常不止一个），由它们的执行"
            "结果产生答复。\n"
            "重要：这是一次性规划，不是分步对话——系统不会把工具结果回传给你，也不会"
            "再次调用你。因此绝不要“先选一个、等结果再决定下一个”；哪怕你的思路是"
            "“先读遥测、再查手册、然后诊断”，也必须把这几步在本次一起列出。\n"
            "规则如下：\n"
            "1. 必须以“产出答复”的工具收尾。只读遥测或检索手册只是收集证据，不会向"
            "操作员展示任何内容。任何非空计划都必须包含下列“产出答复”工具之一，并作为"
            f"最后一步：{answer_list}。要与证据类工具放进同一份计划——它排在证据工具"
            "之后执行、并自动获得它们的结果，因此你无需先看到证据再选它。\n"
            "2. 求全不求少：把答复所需的每个能力都选上，但不要选冗余的；不要只收集"
            "证据就停下。\n"
            "3. 遇到“有什么故障/问题”或“怎么处理/怎么修”类问题，本次计划至少要同时"
            "包含“检索手册”和“运行诊断”两个工具，不要只做其中一步就停；当用户要求"
            "动手处理或执行时，再加上动作建议工具。设备当前状态（型号、故障码等）已在"
            "下方给出，无需专门去读取。\n"
            "4. 优先只读诊断与方案建议。仅当用户明确要求执行某项改变设备状态的操作时，"
            "才选择会改变状态的能力；该操作仍受策略与操作员审批约束。\n"
            "5. 仅当用户询问设备当前状况、或在给出某项动作方案之前，才做状态摘要。对于"
            "问候语或与设备无关的消息，完全不要选择任何工具，好让助手以对话方式回复、"
            "不带机器状态前言。\n"
            "6. 参数必须严格符合各工具的 schema；不要臆造工具或参数。\n"
            + budget
            + f"当前目标设备：型号={machine.model}，machine_id={machine.machine_id}，"
            f"状态={machine.operating_state}，故障码={list(machine.fault_codes)}。"
        )


def _artifact_trace_view(artifact: Artifact) -> dict[str, object]:
    """Compact, JSON-safe view of a step artifact for its trace span.

    Evidence-gathering steps (retrieve/observe) put their results in artifacts
    and leave ``content`` empty, so a span that records only status+content
    makes them look like empty no-ops. ``default=str``/``skipkeys`` guarantee
    serialisation never raises on exotic payloads, so surfacing artifacts can't
    silently drop the whole step output; large payloads collapse to a bounded
    string while small ones keep their structure for readable rendering.
    """
    serialized = json.dumps(
        artifact.data, default=str, ensure_ascii=False, skipkeys=True
    )
    data: object = (
        serialized[:4000] + "…(truncated)"
        if len(serialized) > 4000
        else json.loads(serialized)
    )
    return {
        "artifact_type": artifact.artifact_type,
        "source_capability": artifact.source_capability,
        "confidence": artifact.confidence,
        "data": data,
    }


def _step_input_view(
    step: TaskStep, context: CapabilityExecutionContext
) -> dict[str, object] | None:
    """Trace ``input`` for a step span: declared arguments + upstream evidence.

    Arguments alone under-describe evidence-consuming capabilities such as
    ``diagnostic.agent``, which take no arguments yet run entirely off the
    artifacts their dependencies produced (accumulated into the execution
    context by the time the step runs). Recording only ``arguments`` leaves those
    steps showing a bare ``undefined`` input; folding in ``dependency_artifacts``
    makes the real upstream evidence the step consumed visible on the span.
    """
    view: dict[str, object] = {}
    if step.arguments:
        view["arguments"] = dict(step.arguments)
    evidence = context.dependency_artifacts(step)
    if evidence:
        view["depends_on"] = list(step.depends_on)
        view["evidence"] = [_artifact_trace_view(artifact) for artifact in evidence]
    return view or None


class PlanExecutor:
    """Execute a validated dependency graph through capability providers."""

    def __init__(
        self,
        registry: CapabilityRegistry,
        policy: PolicyEngine,
        events: EventRecorder,
    ) -> None:
        self._registry = registry
        self._policy = policy
        self._events = events

    async def execute(
        self,
        plan: TaskPlan,
        *,
        utterance: Utterance,
        machine: MachineSnapshot,
        approved: bool = False,
    ) -> AssistantResponse:
        self._validate(plan)
        context = CapabilityExecutionContext(utterance, machine)
        completed: set[str] = set()
        results: list[StepResult] = []
        pending = list(plan.steps)
        await self._events.record(
            "plan/created",
            utterance.session_id,
            plan_id=plan.plan_id,
            steps=[step.capability for step in plan.steps],
            reason=plan.reason,
        )

        while pending:
            progress = False
            for step in tuple(pending):
                if not set(step.depends_on).issubset(completed):
                    continue
                pending.remove(step)
                progress = True
                result = await self._execute_step(
                    plan,
                    step,
                    context,
                    approved=approved,
                )
                results.append(result)
                context.artifacts[step.step_id] = result.artifacts
                if result.status == "approval_required":
                    return self._response(results, plan, approved=approved)
                await self._events.record(
                    "plan/step_completed",
                    utterance.session_id,
                    plan_id=plan.plan_id,
                    step_id=step.step_id,
                    capability=step.capability,
                    status=result.status,
                    artifact_ids=[artifact.artifact_id for artifact in result.artifacts],
                )
                if result.status not in {"completed", "skipped"}:
                    return self._response(results, plan, approved=approved)
                completed.add(step.step_id)
            if not progress:
                return AssistantResponse(
                    "任务计划存在未满足的步骤依赖，无法安全执行。",
                    ResponseCategory.ERROR,
                    metadata={"reason": "invalid_plan_dependencies", "plan_id": plan.plan_id},
                )
        return self._response(results, plan, approved=approved)

    async def _execute_step(
        self,
        plan: TaskPlan,
        step: TaskStep,
        context: CapabilityExecutionContext,
        *,
        approved: bool,
    ) -> StepResult:
        with tracing.span(
            f"step:{step.capability}",
            input=_step_input_view(step, context),
            metadata={"step_id": step.step_id},
        ) as scope:
            result = await self._run_step(plan, step, context, approved=approved)
            output: dict[str, object] = {
                "status": result.status,
                "content": result.content[:1000],
            }
            if result.artifacts:
                output["artifacts"] = [
                    _artifact_trace_view(artifact) for artifact in result.artifacts
                ]
            scope.output = output
            if result.status in {"failed", "denied"}:
                scope.level = "ERROR"
                scope.status_message = result.content[:200]
            return result

    async def _run_step(
        self,
        plan: TaskPlan,
        step: TaskStep,
        context: CapabilityExecutionContext,
        *,
        approved: bool,
    ) -> StepResult:
        await self._events.record(
            "plan/step_started",
            context.utterance.session_id,
            plan_id=plan.plan_id,
            step_id=step.step_id,
            capability=step.capability,
        )
        descriptor = self._registry.descriptor(step.capability)
        decision = self._policy.evaluate(descriptor, approved=approved)
        if decision == "approval_required":
            result = StepResult(
                step.step_id,
                step.capability,
                "approval_required",
                "该步骤会改变设备状态，需要操作员确认后才能执行。",
            )
            await self._events.record(
                "plan/approval_required",
                context.utterance.session_id,
                plan_id=plan.plan_id,
                step_id=step.step_id,
                capability=step.capability,
            )
            return result
        if decision != "allow":
            return StepResult(step.step_id, step.capability, "denied", decision)
        try:
            capability_result = await self._registry.execute(step, context)
            return StepResult(
                step.step_id,
                step.capability,
                capability_result.status,
                capability_result.content,
                capability_result.artifacts,
                capability_result.response_category,
                capability_result.metadata,
            )
        except Exception as exc:
            return StepResult(
                step.step_id,
                step.capability,
                "failed",
                f"步骤执行失败：{type(exc).__name__}",
            )

    def _validate(self, plan: TaskPlan) -> None:
        ids = [step.step_id for step in plan.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("task plan contains duplicate step ids")
        known = set(ids)
        for step in plan.steps:
            if not set(step.depends_on).issubset(known):
                raise ValueError(f"step {step.step_id} has an unknown dependency")

    def _visible_results(
        self, results: Sequence[StepResult], *, approved: bool
    ) -> list[StepResult]:
        # After operator approval the state summary and proposal were already
        # shown on the approval turn, so the confirmation reply is just the
        # execution report: surface only the actuation (act/verify) output.
        if approved:
            actuation = [
                item
                for item in results
                if item.content
                and (d := self._registry.descriptor(item.capability)) is not None
                and d.kind in {"act", "verify"}
            ]
            if actuation:
                return actuation
        return [
            item
            for item in results
            if item.content
            and (
                item.status == "approval_required"
                or (
                    (d := self._registry.descriptor(item.capability)) is not None
                    and d.visible_in_response
                )
            )
        ]

    def _response(
        self,
        results: Sequence[StepResult],
        plan: TaskPlan,
        *,
        approved: bool = False,
    ) -> AssistantResponse:
        approval = next((item for item in results if item.status == "approval_required"), None)
        failed = next((item for item in results if item.status in {"failed", "denied"}), None)
        visible = "\n\n".join(
            item.content for item in self._visible_results(results, approved=approved)
        )
        metadata = {
            "plan_id": plan.plan_id,
            "steps": [item.step_id for item in results],
            "capabilities": [item.capability for item in results],
            "artifacts": [
                {
                    "artifact_id": artifact.artifact_id,
                    "artifact_type": artifact.artifact_type,
                    "source_capability": artifact.source_capability,
                    "confidence": artifact.confidence,
                }
                for item in results
                for artifact in item.artifacts
            ],
        }
        step_metadata = {
            item.step_id: item.metadata for item in results if item.metadata
        }
        if step_metadata:
            metadata["step_metadata"] = step_metadata
        for item in reversed(results):
            if "reason" in item.metadata:
                metadata["reason"] = item.metadata["reason"]
                break
        if approval:
            return AssistantResponse(
                visible or approval.content,
                ResponseCategory.APPROVAL_REQUIRED,
                requires_confirmation=True,
                metadata=metadata,
            )
        if failed:
            metadata["failed_step"] = failed.step_id
            return AssistantResponse(failed.content, ResponseCategory.ERROR, metadata=metadata)
        category = next(
            (
                ResponseCategory(item.response_category)
                for item in reversed(results)
                if item.response_category is not None
            ),
            ResponseCategory.DIAGNOSTIC,
        )
        return AssistantResponse(
            visible or "任务已完成，但没有可展示的结果。",
            category,
            metadata=metadata,
        )


class TaskOrchestrator:
    """Facade used by AgentLoop; it has no fixed intent-lane branch."""

    def __init__(self, planner: TaskPlannerPort, executor: PlanExecutor) -> None:
        self._planner = planner
        self._executor = executor
        self._pending: dict[str, TaskPlan] = {}

    async def handle(self, utterance: Utterance, machine: MachineSnapshot) -> AssistantResponse:
        pending = self._pending.get(utterance.session_id)
        if pending is not None and self._is_confirmation(utterance.text):
            response = await self._executor.execute(
                pending,
                utterance=utterance,
                machine=machine,
                approved=True,
            )
            if response.category is not ResponseCategory.APPROVAL_REQUIRED:
                self._pending.pop(utterance.session_id, None)
            return response
        with tracing.span("plan.build", input=utterance.text) as scope:
            plan = await self._planner.build(utterance, machine)
            scope.output = {
                "reason": plan.reason,
                "steps": [step.capability for step in plan.steps],
            }
        response = await self._executor.execute(plan, utterance=utterance, machine=machine)
        if response.category is ResponseCategory.APPROVAL_REQUIRED:
            self._pending[utterance.session_id] = plan
        else:
            self._pending.pop(utterance.session_id, None)
        return response

    @staticmethod
    def _is_confirmation(text: str) -> bool:
        normalized = text.casefold().strip()
        return any(
            phrase in normalized
            for phrase in ("确认", "确认执行", "同意", "执行吧", "yes", "approve", "proceed")
        )
