from __future__ import annotations

from typing import Any

from cat_assistant.domain.models import DomainEvent
from cat_assistant.domain.ports import EventStorePort


class EventRecorder:
    def __init__(self, store: EventStorePort) -> None:
        self._store = store

    async def record(
        self,
        event_type: str,
        session_id: str,
        **payload: Any,
    ) -> None:
        await self._store.append(
            DomainEvent(
                event_type=event_type,
                session_id=session_id,
                payload=payload,
            )
        )

