from __future__ import annotations

from collections.abc import Sequence
import re

from cat_assistant.domain.models import KnowledgeHit, MachineSnapshot


class InMemoryKnowledge:
    """Development adapter; a local hybrid index can implement the same port."""

    def __init__(self, documents: Sequence[KnowledgeHit]) -> None:
        self._documents = tuple(documents)

    async def search(
        self,
        query: str,
        *,
        machine: MachineSnapshot,
        limit: int = 3,
    ) -> Sequence[KnowledgeHit]:
        # Keep the demo index dependency-free while still making fault-code
        # retrieval meaningful. Machine model/fault codes receive a relevance
        # boost, followed by query token overlap.
        tokens = {token.casefold() for token in re.findall(r"[\w-]+", query) if len(token) > 1}
        required = {machine.model.casefold(), *(code.casefold() for code in machine.fault_codes)}
        ranked = []
        for index, hit in enumerate(self._documents):
            haystack = f"{hit.content} {hit.source}".casefold()
            # Explicit query terms (especially a fault code) should win over
            # the machine's default-fault boost when an operator asks about a
            # different code.
            score = hit.score + sum(3.0 for token in tokens if token in haystack)
            score += sum(2.0 for token in required if token and token in haystack)
            ranked.append((score, -index, hit))
        ranked.sort(reverse=True, key=lambda item: (item[0], item[1]))
        return tuple(item[2] for item in ranked[:limit])


def demo_documents() -> tuple[KnowledgeHit, ...]:
    return (
        KnowledgeHit(
            content=(
                "Cat 306 CR 故障码 E123 表示演示用液压压力异常。"
                "检查前应停机、释放液压压力，并由授权技师按手册步骤诊断。"
            ),
            source="Cat 306 CR Service Manual §8.4 (demo)",
            score=0.94,
            document_version="2026.1-demo",
        ),
        KnowledgeHit(
            "Cat 306 CR 故障码 E456 表示冷却液温度过高。降低负载并观察温度；必要时安全停机。冷却后检查冷却液液位、散热器堵塞、风扇皮带和温度传感器。禁止热机立即打开散热器盖。",
            "Cat 306 CR Service Manual §6.2 (demo)", .93, "2026.1-demo",
        ),
        KnowledgeHit(
            "Cat 950 GC 故障码 E45 表示发动机冷却系统温度信号异常。停机冷却后检查冷却液、线束和传感器；若温度持续升高，不得继续重载作业。",
            "Cat 950 GC Service Manual §6.1 (demo)", .92, "2026.1-demo",
        ),
        KnowledgeHit(
            "Cat 950 GC 故障码 F210 表示燃油系统压力偏低。检查燃油液位、燃油滤清器和供油管路是否进气或堵塞；不得在未确认压力正常前反复起动发动机。",
            "Cat 950 GC Service Manual §7.3 (demo)", .91, "2026.1-demo",
        ),
        KnowledgeHit(
            "故障码处理通用安全要求：先确认设备处于安全状态，停止危险动作并记录故障发生时间、负载和环境；涉及液压、燃油或高温系统时由授权技师执行检修。",
            "CAT Safety Handbook §1 (demo)", .80, "2026.1-demo",
        ),
        KnowledgeHit(
            "Cat 306 CR 维护提醒：接近计划小时数时安排停机保养，检查发动机油、液压油、滤芯和润滑点，并记录更换件批次。",
            "Cat 306 CR Maintenance Schedule (demo)", .78, "2026.1-demo",
        ),
    )
