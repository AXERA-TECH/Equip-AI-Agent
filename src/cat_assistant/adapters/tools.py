from __future__ import annotations

import json

from cat_assistant.domain.models import ToolExecutionContext, ToolSpec
from cat_assistant.domain.ports import KnowledgePort


class MachineStatusTool:
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="get_machine_status",
            description="Read the current machine snapshot. This tool cannot control the machine.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        )

    async def execute(
        self,
        arguments: dict[str, object],
        context: ToolExecutionContext,
    ) -> str:
        del arguments
        snapshot = context.snapshot
        return json.dumps(
            {
                "model": snapshot.model,
                "state": snapshot.operating_state,
                "engine_running": snapshot.engine_running,
                "hour_meter": snapshot.hour_meter,
                "fuel_percent": snapshot.fuel_percent,
                "fault_codes": snapshot.fault_codes,
                "captured_at": snapshot.captured_at.isoformat(),
            },
            ensure_ascii=False,
        )


class ManualSearchTool:
    def __init__(self, knowledge: KnowledgePort) -> None:
        self._knowledge = knowledge

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="search_manual",
            description="Search locally installed, machine-compatible manuals.",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string", "minLength": 1}},
                "required": ["query"],
                "additionalProperties": False,
            },
        )

    async def execute(
        self,
        arguments: dict[str, object],
        context: ToolExecutionContext,
    ) -> str:
        query = str(arguments.get("query", "")).strip()
        if not query:
            raise ValueError("query is required")
        hits = await self._knowledge.search(query, machine=context.snapshot)
        return json.dumps(
            [
                {
                    "content": hit.content,
                    "source": hit.source,
                    "score": hit.score,
                    "document_version": hit.document_version,
                }
                for hit in hits
            ],
            ensure_ascii=False,
        )

