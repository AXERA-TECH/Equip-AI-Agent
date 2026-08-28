from __future__ import annotations

import logging
from collections.abc import Callable

from cat_assistant.application.loop import AgentLoop
from cat_assistant.channels.base import ChannelMessage, MessageChannel
from cat_assistant.domain.models import Utterance

log = logging.getLogger(__name__)


class ChannelBridge:
    """Connect a channel to ``AgentLoop`` with stable session scoping.

    ``machine_id`` can be a fixed device or a resolver for multi-device chats.
    The sender id is used as operator id so history cannot leak between users.
    """

    def __init__(self, app: AgentLoop, *, machine_id: str | Callable[[ChannelMessage], str]):
        self.app = app
        self.machine_id = machine_id

    async def dispatch(self, channel: MessageChannel, message: ChannelMessage) -> None:
        if not message.text.strip():
            return
        machine = self.machine_id(message) if callable(self.machine_id) else self.machine_id
        session_id = f"{message.channel}:{message.chat_id}"
        response = await self.app.handle(Utterance(
            text=message.text,
            session_id=session_id,
            machine_id=machine,
            operator_id=f"{message.channel}:{message.sender_id}",
        ))
        try:
            await channel.send_text(message.chat_id, response.text, reply_to=message.message_id)
        except Exception:
            log.exception("failed to deliver %s reply", message.channel)

    async def dispatch_update(self, channel: MessageChannel, payload: object) -> bool:
        message = await channel.handle_update(payload)
        if message is None:
            return False
        await self.dispatch(channel, message)
        return True
