"""Deterministic, multi-step equipment scenarios for offline demonstrations.

The scenarios deliberately contain more signals than :class:`MachineSnapshot`
currently exposes.  The snapshot is the compatibility view consumed by the
existing agent, while ``SimulationSample`` gives a future telemetry adapter a
realistic time-series to replay.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping

from cat_assistant.domain.models import KnowledgeHit, MachineSnapshot


@dataclass(frozen=True, slots=True)
class SimulationSample:
    """One point in a deterministic equipment timeline."""

    observed_at: datetime
    phase: str
    engine_rpm: float
    hydraulic_pressure_bar: float
    coolant_temp_c: float
    fuel_percent: float
    vibration_mm_s: float
    fault_codes: tuple[str, ...] = ()
    operator_note: str = ""

    def snapshot(self, *, machine: MachineSnapshot) -> MachineSnapshot:
        """Project rich simulated signals onto today's domain snapshot."""

        return MachineSnapshot(
            machine_id=machine.machine_id,
            model=machine.model,
            serial_number=machine.serial_number,
            engine_running=self.engine_rpm > 0,
            operating_state=self.phase,
            hour_meter=machine.hour_meter,
            next_service_hours=machine.next_service_hours,
            fuel_percent=self.fuel_percent,
            fault_codes=self.fault_codes,
            captured_at=self.observed_at,
        )


@dataclass(frozen=True, slots=True)
class SimulationScenario:
    """A named incident, its replayable samples and matching local evidence."""

    scenario_id: str
    title: str
    description: str
    machine: MachineSnapshot
    samples: tuple[SimulationSample, ...]
    documents: tuple[KnowledgeHit, ...]

    def sample(self, phase: int = 0) -> SimulationSample:
        if not self.samples:
            raise ValueError(f"scenario {self.scenario_id!r} has no samples")
        return self.samples[max(0, min(phase, len(self.samples) - 1))]

    def snapshot(self, phase: int = 0) -> MachineSnapshot:
        return self.sample(phase).snapshot(machine=self.machine)


def _times(start: datetime, count: int, minutes: int = 5) -> tuple[datetime, ...]:
    return tuple(start + timedelta(minutes=minutes * index) for index in range(count))


def simulation_catalog() -> Mapping[str, SimulationScenario]:
    """Return the built-in incident catalog.

    Values are fixed so screenshots, demos and regression tests remain
    reproducible.  All text is explicitly marked as demo/synthetic evidence.
    """

    base = datetime(2026, 8, 19, 8, 0, tzinfo=timezone.utc)
    machine = MachineSnapshot(
        machine_id="cat-306-demo",
        model="Cat 306 CR",
        serial_number="DEMO-306-001",
        engine_running=True,
        operating_state="怠速",
        hour_meter=1248.2,
        next_service_hours=1250.0,
        fuel_percent=68.0,
        fault_codes=("E123",),
        captured_at=base,
    )
    hydraulic_times = _times(base, 5)
    hydraulic = SimulationScenario(
        scenario_id="hydraulic-pressure-drift",
        title="液压压力逐步下降",
        description="从轻微压力漂移发展为 E123，适合演示观测、检索、诊断和审批。",
        machine=machine,
        samples=(
            SimulationSample(hydraulic_times[0], "作业中", 1850, 248, 78, 68, 1.8),
            SimulationSample(hydraulic_times[1], "压力波动", 1840, 221, 81, 67, 2.6, ("E123",), "操作员报告动作变慢"),
            SimulationSample(hydraulic_times[2], "报警", 1820, 188, 84, 66, 3.4, ("E123",), "黄色报警灯亮，听到蜂鸣声"),
            SimulationSample(hydraulic_times[3], "安全停机", 0, 0, 83, 66, 0.9, ("E123",), "已停机并释放残余压力"),
            SimulationSample(hydraulic_times[4], "待检修", 0, 0, 71, 65, 0.7, ("E123",), "等待授权技师检查"),
        ),
        documents=(
            KnowledgeHit("Cat 306 CR E123 表示液压压力异常。先停机并释放液压压力，再检查液压油液位、滤芯压差和压力传感器。", "Cat 306 CR Service Manual §8.4 (synthetic)", .96, "2026.1-sim"),
            KnowledgeHit("若压力在负载下持续低于 210 bar，应停止继续作业；不得在未释放压力时拆卸管路。", "Hydraulic Safety Bulletin §3.2 (synthetic)", .92, "2026.1-sim"),
        ),
    )

    thermal_times = _times(base + timedelta(hours=1), 5)
    thermal = SimulationScenario(
        scenario_id="coolant-overheat",
        title="冷却液温度持续升高",
        description="温度、风扇转速和负载共同变化，适合演示趋势判断与安全降级。",
        machine=MachineSnapshot(
            machine_id=machine.machine_id, model=machine.model,
            serial_number=machine.serial_number, engine_running=True,
            operating_state="重载作业", hour_meter=machine.hour_meter,
            next_service_hours=machine.next_service_hours, fuel_percent=54.0,
            fault_codes=("E456",), captured_at=thermal_times[0],
        ),
        samples=(
            SimulationSample(thermal_times[0], "重载作业", 2100, 246, 86, 54, 2.0),
            SimulationSample(thermal_times[1], "温度升高", 2140, 243, 94, 53, 2.2),
            SimulationSample(thermal_times[2], "高温报警", 2180, 239, 103, 52, 2.5, ("E456",), "冷却液温度报警"),
            SimulationSample(thermal_times[3], "降载运行", 1500, 220, 99, 52, 1.9, ("E456",), "操作员降低发动机负载"),
            SimulationSample(thermal_times[4], "等待冷却", 0, 0, 91, 51, 0.8, ("E456",), "发动机已停止，等待冷却"),
        ),
        documents=(
            KnowledgeHit("E456 表示冷却液温度过高。高温报警时应降低负载，必要时停机冷却，检查散热器堵塞、风扇皮带和冷却液液位。", "Cat 306 CR Service Manual §6.2 (synthetic)", .95, "2026.1-sim"),
            KnowledgeHit("禁止在高温状态下立即打开散热器盖。应等待冷却液温度降至安全范围。", "Thermal Safety Bulletin §2.1 (synthetic)", .94, "2026.1-sim"),
        ),
    )

    fuel_times = _times(base + timedelta(hours=3), 4, minutes=30)
    fuel = SimulationScenario(
        scenario_id="fuel-and-service-due",
        title="低燃油叠加临近保养",
        description="两个低风险事项同时出现，适合演示优先级排序和行动建议。",
        machine=MachineSnapshot(
            machine_id=machine.machine_id, model=machine.model, serial_number=machine.serial_number,
            engine_running=True, operating_state="作业中", hour_meter=1348.6,
            next_service_hours=1350.0, fuel_percent=18.0, fault_codes=(), captured_at=fuel_times[0],
        ),
        samples=(
            SimulationSample(fuel_times[0], "作业中", 1900, 244, 79, 18, 1.6),
            SimulationSample(fuel_times[1], "低燃油提醒", 1880, 241, 80, 14, 1.7, (), "燃油低于 15%"),
            SimulationSample(fuel_times[2], "接近保养", 1750, 239, 80, 10, 1.8, (), "距离保养计划不足 1 小时"),
            SimulationSample(fuel_times[3], "待补给保养", 0, 0, 76, 9, 0.8, (), "设备停在维护区"),
        ),
        documents=(
            KnowledgeHit("Cat 306 CR 燃油低于 15% 时应安排补给，避免燃油系统吸入空气。", "Operator Handbook §2.3 (synthetic)", .91, "2026.1-sim"),
            KnowledgeHit("1250 小时保养包括发动机油、液压油滤芯和润滑点检查；应在计划小时数前安排停机维护。", "Maintenance Schedule §1250h (synthetic)", .93, "2026.1-sim"),
        ),
    )

    return {item.scenario_id: item for item in (hydraulic, thermal, fuel)}


def get_simulation_scenario(name: str) -> SimulationScenario:
    try:
        return simulation_catalog()[name]
    except KeyError as exc:
        choices = ", ".join(sorted(simulation_catalog()))
        raise ValueError(f"unknown simulation scenario {name!r}; choose one of: {choices}") from exc


class ScenarioTelemetry:
    """Replay one scenario while satisfying the existing telemetry port."""

    def __init__(self, scenario: SimulationScenario, *, phase: int = 0) -> None:
        self.scenario = scenario
        self.phase = phase

    async def get_snapshot(self, machine_id: str) -> MachineSnapshot:
        if machine_id != self.scenario.machine.machine_id:
            raise LookupError(f"unknown machine: {machine_id}")
        return self.scenario.snapshot(self.phase)

    def advance(self) -> SimulationSample:
        self.phase = min(self.phase + 1, len(self.scenario.samples) - 1)
        return self.scenario.sample(self.phase)

    def set_phase(self, phase: int) -> SimulationSample:
        self.phase = max(0, min(phase, len(self.scenario.samples) - 1))
        return self.scenario.sample(self.phase)
