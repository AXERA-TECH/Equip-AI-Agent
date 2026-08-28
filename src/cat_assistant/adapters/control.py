from __future__ import annotations

import dataclasses
from typing import Protocol

from cat_assistant.domain.models import ControlOutcome, MachineSnapshot


class SupportsSnapshotWrite(Protocol):
    def apply_snapshot(self, snapshot: MachineSnapshot) -> None: ...


class SimulatedMachineControl:
    """A virtual Safety Gateway executor for offline demonstrations.

    It closes the propose -> approve -> execute -> verify loop without any real
    actuator: a recognized command mutates the in-memory telemetry snapshot so
    the next read reflects the new state, exactly as a real machine's telemetry
    would catch up after an actuation. It is deliberately the only write path
    and runs only after the deterministic policy gate and operator confirmation.

    The device it acts on is itself simulated (see ``demo_snapshots``); this
    executor simply completes that simulation rather than controlling hardware.
    """

    _STOP_TERMS = (
        "停止",
        "停机",
        "熄火",
        "关闭",
        "关机",
        "shutdown",
        "stop",
        "power off",
        "turn off",
    )
    _START_TERMS = ("启动", "开机", "点火", "start", "power on", "turn on")

    def __init__(self, telemetry: SupportsSnapshotWrite) -> None:
        self._telemetry = telemetry

    async def apply(self, request: str, *, machine: MachineSnapshot) -> ControlOutcome:
        lowered = request.casefold()
        if any(term in lowered for term in self._STOP_TERMS):
            updated = dataclasses.replace(
                machine, engine_running=False, operating_state="熄火"
            )
            self._commit(updated)
            return ControlOutcome(
                accepted=True,
                summary=(
                    f"已通过虚拟安全网关执行：{request}。"
                    "发动机已停止，运行状态更新为「熄火」。"
                ),
                machine=updated,
            )
        if any(term in lowered for term in self._START_TERMS):
            updated = dataclasses.replace(
                machine, engine_running=True, operating_state="怠速"
            )
            self._commit(updated)
            return ControlOutcome(
                accepted=True,
                summary=(
                    f"已通过虚拟安全网关执行：{request}。"
                    "发动机已启动，运行状态更新为「怠速」。"
                ),
                machine=updated,
            )
        # A control request the simulator cannot map to a snapshot field is still
        # accepted and recorded, so the full chain stays connected end to end.
        return ControlOutcome(
            accepted=True,
            summary=(
                f"已通过虚拟安全网关下发指令：{request}。"
                "（仿真执行器已记录该动作，未改变具体遥测字段。）"
            ),
            machine=None,
        )

    def _commit(self, snapshot: MachineSnapshot) -> None:
        self._telemetry.apply_snapshot(snapshot)
