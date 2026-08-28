from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

from cat_assistant.application.capabilities import CapabilityExecutionContext
from cat_assistant.application.context import ContextBuilder
from cat_assistant.application.query import DeterministicQueryService
from cat_assistant.application.runner import BoundedAgentRunner
from cat_assistant.domain.capabilities import (
    Artifact,
    CapabilityDescriptor,
    CapabilityResult,
)
from cat_assistant.domain.models import ChatMessage
from cat_assistant.domain.plans import TaskStep
from cat_assistant.domain.ports import KnowledgePort, MachineControlPort


EMPTY_INPUT = {"type": "object", "properties": {}, "additionalProperties": False}
ARTIFACT_OUTPUT = {"type": "object"}


class TelemetryReadProvider:
    descriptor = CapabilityDescriptor(
        name="telemetry.read",
        version="1.0.0",
        description="读取本轮设备的权威遥测快照（型号、运行状态、故障码、油量、工时等）。",
        input_schema=EMPTY_INPUT,
        output_schema=ARTIFACT_OUTPUT,
        kind="observe",
        source="telemetry",
        produces=("machine_snapshot",),
        # 纯管道：设备快照已随执行上下文提供给每个能力，模型无需专门“读一次”，
        # 故对模型隐藏；规则规划器与依赖图仍照常使用它。
        model_selectable=False,
    )

    async def execute(
        self,
        arguments: dict[str, Any],
        context: CapabilityExecutionContext,
        step: TaskStep,
    ) -> CapabilityResult:
        del arguments, step
        snapshot = context.machine
        data = {
            "machine_id": snapshot.machine_id,
            "model": snapshot.model,
            "serial_number": snapshot.serial_number,
            "state": snapshot.operating_state,
            "engine_running": snapshot.engine_running,
            "hour_meter": snapshot.hour_meter,
            "next_service_hours": snapshot.next_service_hours,
            "fuel_percent": snapshot.fuel_percent,
            "fault_codes": list(snapshot.fault_codes),
            "captured_at": snapshot.captured_at.isoformat(),
        }
        return CapabilityResult(
            artifacts=(
                Artifact(
                    "machine_snapshot",
                    data,
                    self.descriptor.name,
                    provenance={"authority": "machine_telemetry", "machine_id": snapshot.machine_id},
                    observed_at=snapshot.captured_at,
                ),
            )
        )


class TelemetrySummaryProvider:
    descriptor = CapabilityDescriptor(
        name="telemetry.summarize",
        version="1.0.0",
        description="根据当前遥测，生成一句面向操作员的设备状态摘要。",
        input_schema=EMPTY_INPUT,
        output_schema=ARTIFACT_OUTPUT,
        kind="infer",
        source="telemetry",
        consumes=("machine_snapshot",),
        produces=("telemetry_summary",),
        visible_in_response=True,
    )

    async def execute(
        self,
        arguments: dict[str, Any],
        context: CapabilityExecutionContext,
        step: TaskStep,
    ) -> CapabilityResult:
        del arguments, step
        faults = "、".join(context.machine.fault_codes) if context.machine.fault_codes else "无活动故障码"
        text = (
            f"{context.machine.model} 当前状态：{context.machine.operating_state}，"
            f"发动机{'运行中' if context.machine.engine_running else '已停止'}，"
            f"燃油 {context.machine.fuel_percent:.0f}%，故障码：{faults}。"
        )
        return CapabilityResult(
            content=text,
            artifacts=(Artifact("telemetry_summary", {"text": text}, self.descriptor.name),),
        )


class UserReportedObservationProvider:
    descriptor = CapabilityDescriptor(
        name="observation.user_report",
        version="1.0.0",
        description="把操作员口述的可见/可听异常（指示灯、报警声、异响等）记录为非权威证据。",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        output_schema=ARTIFACT_OUTPUT,
        kind="observe",
        source="operator",
        trigger_terms=("指示灯", "报警灯", "灯亮", "报警声", "蜂鸣", "异响", "warning light", "alarm"),
        auto_select=True,
        produces=("indicator_report", "audio_alarm_report"),
        # 小众能力：LLM 规划模式下几乎不会被单独选中，且诊断工具已能从用户原文里
        # 读到症状描述。对模型隐藏，规则规划器仍通过 trigger_terms 自动发现它。
        model_selectable=False,
    )

    async def execute(
        self,
        arguments: dict[str, Any],
        context: CapabilityExecutionContext,
        step: TaskStep,
    ) -> CapabilityResult:
        del context, step
        text = str(arguments["text"])
        normalized = text.casefold()
        artifacts = []
        if any(term in normalized for term in ("指示灯", "报警灯", "灯亮", "warning light")):
            artifacts.append(
                Artifact(
                    "indicator_report",
                    {"description": text, "authoritative": False},
                    self.descriptor.name,
                    confidence=0.5,
                    provenance={"reported_by": "operator"},
                )
            )
        if any(term in normalized for term in ("报警声", "蜂鸣", "异响", "alarm")):
            artifacts.append(
                Artifact(
                    "audio_alarm_report",
                    {"description": text, "authoritative": False},
                    self.descriptor.name,
                    confidence=0.5,
                    provenance={"reported_by": "operator"},
                )
            )
        return CapabilityResult(artifacts=tuple(artifacts))


@dataclass(slots=True)
class KnowledgeSearchProvider:
    knowledge: KnowledgePort

    descriptor = CapabilityDescriptor(
        name="knowledge.search",
        version="1.0.0",
        description="在与当前机型匹配的本地知识库与手册中检索资料。",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        output_schema={"type": "array", "items": {"type": "object"}},
        kind="retrieve",
        source="knowledge",
        # Advertise fault/problem intent as auto_select trigger terms so the
        # planner discovers manual retrieval through the same descriptor-driven
        # path as observation providers, rather than depending solely on the
        # planner's hardcoded keyword gate. This closes the gap where a natural
        # fault-status question ("机器存在什么问题吗") uses "问题" instead of
        # "故障" and would otherwise skip retrieval entirely.
        trigger_terms=("问题", "故障", "毛病", "异常", "报警", "隐患"),
        auto_select=True,
        consumes=("machine_snapshot",),
        produces=("knowledge_evidence",),
    )

    async def execute(
        self,
        arguments: dict[str, Any],
        context: CapabilityExecutionContext,
        step: TaskStep,
    ) -> CapabilityResult:
        del step
        hits = await self.knowledge.search(str(arguments["query"]), machine=context.machine)
        data = [
            {
                "content": hit.content,
                "source": hit.source,
                "score": hit.score,
                "document_version": hit.document_version,
            }
            for hit in hits
        ]
        return CapabilityResult(
            artifacts=(
                Artifact(
                    "knowledge_evidence",
                    data,
                    self.descriptor.name,
                    confidence=max((hit.score for hit in hits), default=0.0),
                    provenance={"machine_model": context.machine.model},
                ),
            )
        )


@dataclass(slots=True)
class DeterministicResponseProvider:
    query_service: DeterministicQueryService

    descriptor = CapabilityDescriptor(
        name="response.compose",
        version="1.0.0",
        description="针对简单的设备事实类问题，给出确定性答复。",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        output_schema=ARTIFACT_OUTPUT,
        kind="summarize",
        source="core",
        consumes=("machine_snapshot",),
        produces=("assistant_response",),
        visible_in_response=True,
        # 确定性简答走规则规划器的“简单事实”通道；对模型而言它与 diagnostic.agent
        # 作为收尾工具重叠，暴露反而增加选择歧义，故对模型隐藏。
        model_selectable=False,
    )

    async def execute(
        self,
        arguments: dict[str, Any],
        context: CapabilityExecutionContext,
        step: TaskStep,
    ) -> CapabilityResult:
        del step
        response = await self.query_service.answer(str(arguments["text"]), context.machine)
        return CapabilityResult(
            content=response.text,
            response_category=response.category.value,
            artifacts=(Artifact("assistant_response", {"text": response.text}, self.descriptor.name),),
        )


@dataclass
class DiagnosticAgentProvider:
    runner: BoundedAgentRunner
    context_builder: ContextBuilder
    # Capability-level ceiling for the whole agent run, in seconds. Defaults low
    # for tests/standalone use; bootstrap raises it to the configured turn budget
    # so a slow edge model is not cut off before the operator's model/turn
    # timeouts. The runner still bounds each model call and the loop bounds the
    # turn, so this wrapper is a backstop, not the primary guard.
    timeout_seconds: int = 90

    # Class-level template: static metadata (name/description/model_selectable) is
    # read at the class level (see test_builtin_menu_is_trimmed_...). Each instance
    # rebuilds it in __post_init__ with the configured timeout so the registry's
    # per-capability wait_for tracks configuration instead of a hardcoded ceiling.
    descriptor = CapabilityDescriptor(
        name="diagnostic.agent",
        version="1.0.0",
        description="综合遥测与已收集的证据，给出有界限的故障诊断结论。",
        input_schema=EMPTY_INPUT,
        output_schema=ARTIFACT_OUTPUT,
        kind="infer",
        source="core",
        timeout_seconds=90,
        visible_in_response=True,
        consumes=("machine_snapshot", "knowledge_evidence"),
        produces=("diagnostic_assessment",),
    )

    def __post_init__(self) -> None:
        # Shadow the class template with an instance descriptor carrying the
        # configured timeout. Class-level access still returns the template.
        self.descriptor = replace(
            type(self).descriptor, timeout_seconds=self.timeout_seconds
        )

    async def execute(
        self,
        arguments: dict[str, Any],
        context: CapabilityExecutionContext,
        step: TaskStep,
    ) -> CapabilityResult:
        del arguments
        messages = list(
            await self.context_builder.build(
                session_id=context.utterance.session_id,
                user_text=context.utterance.text,
                machine=context.machine,
                operator_id=context.utterance.operator_id,
            )
        )
        # Evidence must be spliced in BEFORE the final user turn, never appended
        # after it. ContextBuilder always puts the current request last; appending
        # system messages after it yields a ``...user, system, <assistant>`` turn
        # order that is out-of-distribution for ChatML. Small edge models (observed
        # on Qwen3 2B via AXLLM) then echo a fake ``<|im_start|>user`` turn instead
        # of answering, which _strip_role_leakage collapses to an empty reply.
        # Popping the user turn and re-appending it keeps a standard "system context
        # + user question" prompt so the model actually responds.
        user_turn = messages.pop()
        evidence = context.dependency_artifacts(step)
        reported = [
            artifact for artifact in evidence if artifact.artifact_type.endswith("_report")
        ]
        if reported:
            messages.append(
                ChatMessage(
                    role="system",
                    content=(
                        "Operator-reported observations are non-authoritative and must be "
                        "confirmed before a high-confidence conclusion: "
                        + json.dumps([artifact.data for artifact in reported], ensure_ascii=False)
                    ),
                )
            )
        knowledge = next(
            (artifact for artifact in evidence if artifact.artifact_type == "knowledge_evidence"),
            None,
        )
        if knowledge is not None:
            payload = json.dumps(knowledge.data, ensure_ascii=False)
            messages.append(
                ChatMessage(
                    role="system",
                    content=(
                        "Collected knowledge evidence from the knowledge.search "
                        "capability. This is trusted task evidence, not a tool-call "
                        "message. Cite its sources and do not claim that you performed "
                        "another search unless you actually do so:\n"
                        + payload
                    ),
                )
            )
        messages.append(user_turn)
        response = await self.runner.run(
            session_id=context.utterance.session_id,
            user_text=context.utterance.text,
            machine=context.machine,
            operator_id=context.utterance.operator_id,
            initial_messages=messages,
        )
        return CapabilityResult(
            status=(
                "failed"
                if response.category.value == "error"
                else "completed"
            ),
            content=response.text,
            response_category=response.category.value,
            artifacts=(
                Artifact(
                    "diagnostic_assessment",
                    {"text": response.text, "evidence_ids": [item.artifact_id for item in evidence]},
                    self.descriptor.name,
                    provenance={"bounded": True},
                ),
            ),
            metadata=response.metadata,
        )


class ActionProposalProvider:
    descriptor = CapabilityDescriptor(
        name="action.propose",
        version="1.0.0",
        description="给出一套需操作员复核的处置/维修步骤建议。",
        input_schema={
            "type": "object",
            "properties": {"request": {"type": "string"}},
            "required": ["request"],
            "additionalProperties": False,
        },
        output_schema=ARTIFACT_OUTPUT,
        kind="propose",
        source="core",
        visible_in_response=True,
        consumes=("diagnostic_assessment",),
        produces=("action_proposal",),
    )

    async def execute(
        self,
        arguments: dict[str, Any],
        context: CapabilityExecutionContext,
        step: TaskStep,
    ) -> CapabilityResult:
        del step
        faults = ", ".join(context.machine.fault_codes) or "未发现活动故障码"
        text = (
            f"建议执行方案：先确认设备处于安全工况（当前：{context.machine.operating_state}），"
            f"核对故障码 {faults}，再由授权操作员执行请求：{arguments['request']}。"
        )
        return CapabilityResult(
            content=text,
            artifacts=(Artifact("action_proposal", {"text": text, "request": arguments["request"]}, self.descriptor.name),),
        )


class MachineControlProvider:
    descriptor = CapabilityDescriptor(
        name="machine.control",
        version="1.0.0",
        description="经安全网关审批后，执行改变设备状态的操作。",
        input_schema={
            "type": "object",
            "properties": {"request": {"type": "string"}},
            "required": ["request"],
            "additionalProperties": False,
        },
        output_schema=ARTIFACT_OUTPUT,
        kind="act",
        risk_level="critical",
        side_effect="device_state_change",
        requires_approval=True,
        source="safety-gateway",
        visible_in_response=True,
        consumes=("action_proposal",),
        produces=("action_result",),
    )

    def __init__(self, executor: MachineControlPort | None = None) -> None:
        self._executor = executor

    async def execute(
        self,
        arguments: dict[str, Any],
        context: CapabilityExecutionContext,
        step: TaskStep,
    ) -> CapabilityResult:
        del step
        if self._executor is None:
            # Safe default: with no executor bound, a state change cannot be
            # performed. This runs only after policy + approval, so denying here
            # is the last line of defense rather than the primary gate.
            return CapabilityResult(
                status="denied",
                content="当前没有接入受安全网关保护的设备控制执行器。",
            )
        request = arguments["request"]
        outcome = await self._executor.apply(request, machine=context.machine)
        if not outcome.accepted:
            return CapabilityResult(status="denied", content=outcome.summary)
        data: dict[str, Any] = {"request": request, "summary": outcome.summary}
        if outcome.machine is not None:
            data["operating_state"] = outcome.machine.operating_state
            data["engine_running"] = outcome.machine.engine_running
        return CapabilityResult(
            status="completed",
            content=outcome.summary,
            artifacts=(Artifact("action_result", data, self.descriptor.name),),
        )


def builtin_capability_providers(
    *,
    knowledge: KnowledgePort,
    query_service: DeterministicQueryService,
    agent_runner: BoundedAgentRunner,
    context_builder: ContextBuilder,
    machine_control: MachineControlPort | None = None,
    diagnostic_timeout_seconds: int = 90,
) -> tuple[object, ...]:
    return (
        TelemetryReadProvider(),
        TelemetrySummaryProvider(),
        UserReportedObservationProvider(),
        KnowledgeSearchProvider(knowledge),
        DeterministicResponseProvider(query_service),
        DiagnosticAgentProvider(
            agent_runner, context_builder, timeout_seconds=diagnostic_timeout_seconds
        ),
        ActionProposalProvider(),
        MachineControlProvider(machine_control),
    )
