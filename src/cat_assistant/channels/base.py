from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol


@dataclass(frozen=True, slots=True)
class ChannelMessage:
    """Normalized inbound message shared by all chat platforms."""

    channel: str
    chat_id: str
    sender_id: str
    text: str
    message_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class MessageChannel(Protocol):
    name: str

    async def send_text(self, chat_id: str, text: str, *, reply_to: str | None = None) -> None: ...

    async def handle_update(self, payload: Any) -> ChannelMessage | None: ...


MessageHandler = Callable[[ChannelMessage], Awaitable[None]]
