from __future__ import annotations

from cat_assistant.domain.models import MachineSnapshot


class InMemoryTelemetry:
    """Development adapter standing in for a read-only CAN/J1939 gateway."""

    def __init__(self, snapshots: dict[str, MachineSnapshot]) -> None:
        self._snapshots = snapshots

    async def get_snapshot(self, machine_id: str) -> MachineSnapshot:
        try:
            return self._snapshots[machine_id]
        except KeyError as exc:
            raise LookupError(f"unknown machine: {machine_id}") from exc

    def list_snapshots(self) -> tuple[MachineSnapshot, ...]:
        """Enumerate every known machine so hosts can offer a device picker."""
        return tuple(self._snapshots.values())

    def apply_snapshot(self, snapshot: MachineSnapshot) -> None:
        """Replace a machine's authoritative snapshot.

        Used by the simulated control executor so an actuation is reflected by
        the next read, mirroring how a real machine's telemetry catches up after
        a state change. Reads remain the single source of truth.
        """
        self._snapshots[snapshot.machine_id] = snapshot


def demo_snapshot(machine_id: str = "cat-306-demo") -> MachineSnapshot:
    return MachineSnapshot(
        machine_id=machine_id,
        model="Cat 306 CR",
        serial_number="DEMO-306-001",
        engine_running=True,
        operating_state="怠速",
        hour_meter=1248.2,
        next_service_hours=1250.0,
        fuel_percent=68.0,
        fault_codes=("E123",),
    )


def demo_snapshots() -> dict[str, MachineSnapshot]:
    """A small fleet of demo machines with varied models and health states."""
    fleet = (
        demo_snapshot(),
        MachineSnapshot(
            machine_id="cat-320-demo",
            model="Cat 320",
            serial_number="DEMO-320-014",
            engine_running=True,
            operating_state="作业中",
            hour_meter=5321.0,
            next_service_hours=5400.0,
            fuel_percent=82.0,
            fault_codes=(),
        ),
        MachineSnapshot(
            machine_id="cat-950gc-demo",
            model="Cat 950 GC",
            serial_number="DEMO-950-007",
            engine_running=False,
            operating_state="熄火",
            hour_meter=8123.5,
            next_service_hours=8130.0,
            fuel_percent=14.0,
            fault_codes=("E45", "F210"),
        ),
        MachineSnapshot(
            machine_id="cat-d6-demo",
            model="Cat D6",
            serial_number="DEMO-D6-021",
            engine_running=True,
            operating_state="怠速",
            hour_meter=3102.7,
            next_service_hours=3150.0,
            fuel_percent=47.0,
            fault_codes=(),
        ),
    )
    return {snapshot.machine_id: snapshot for snapshot in fleet}
