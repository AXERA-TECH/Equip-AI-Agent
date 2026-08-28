from __future__ import annotations

from typing import Any

from cat_assistant.channels.base import ChannelMessage
from cat_assistant.channels.http import post_json


class FeishuChannel:
    """Feishu/Lark bot webhook adapter; inbound payloads are gateway-normalized."""

    name = "feishu"

    def __init__(self, webhook_url: str, *, timeout: float = 10):
        self.webhook_url, self.timeout = webhook_url, timeout

    async def handle_update(self, payload: Any) -> ChannelMessage | None:
        if not isinstance(payload, dict): return None
        event = payload.get("event", payload)
        text = event.get("text") or event.get("content")
        if not isinstance(text, str) or not text.strip(): return None
        return ChannelMessage(self.name, str(event.get("chat_id", event.get("open_chat_id", ""))), str(event.get("sender_id", "")), text, str(event.get("message_id", "")), event)

    async def send_text(self, chat_id: str, text: str, *, reply_to: str | None = None) -> None:
        await post_json(self.webhook_url, {"msg_type": "text", "content": {"text": text}}, timeout=self.timeout)
