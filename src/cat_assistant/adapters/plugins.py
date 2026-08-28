from __future__ import annotations

from cat_assistant.application.plugins import PluginContext, PluginManifest
from cat_assistant.adapters.tools import MachineStatusTool, ManualSearchTool


class ReadOnlyEquipmentToolsPlugin:
    """Built-in plugin proving that tools no longer need bootstrap wiring."""

    manifest = PluginManifest(
        name="cat.readonly-equipment-tools",
        version="0.1.0",
        description="Machine status and local manual search tools",
    )

    def register(self, context: PluginContext) -> None:
        knowledge = context.services.require("knowledge")
        context.tools.register(
            MachineStatusTool(),
            owner=self.manifest.name,
        )
        context.tools.register(
            ManualSearchTool(knowledge),
            owner=self.manifest.name,
        )

    def shutdown(self, context: PluginContext) -> None:
        del context


def create_plugin() -> ReadOnlyEquipmentToolsPlugin:
    return ReadOnlyEquipmentToolsPlugin()

