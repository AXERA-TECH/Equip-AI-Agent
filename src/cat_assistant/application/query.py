from __future__ import annotations

from cat_assistant.domain.models import AssistantResponse, MachineSnapshot, ResponseCategory


class DeterministicQueryService:
    """Fast path for facts that do not require generative reasoning."""

    async def answer(self, text: str, machine: MachineSnapshot) -> AssistantResponse:
        lowered = text.casefold()
        if "保养" in text or "service" in lowered:
            remaining = max(0.0, machine.next_service_hours - machine.hour_meter)
            return AssistantResponse(
                (
                    f"当前小时数 {machine.hour_meter:.1f}，下一次保养计划在 "
                    f"{machine.next_service_hours:.1f} 小时，约剩 {remaining:.1f} 小时。"
                ),
                ResponseCategory.INFORMATION,
            )

        faults = "、".join(machine.fault_codes) if machine.fault_codes else "无活动故障码"
        return AssistantResponse(
            (
                f"{machine.model} 当前状态：{machine.operating_state}，"
                f"燃油 {machine.fuel_percent:.0f}%，{faults}。"
            ),
            ResponseCategory.INFORMATION,
        )

