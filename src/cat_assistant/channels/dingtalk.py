from __future__ import annotations

from typing import Any

from cat_assistant.channels.base import ChannelMessage
from cat_assistant.channels.http import post_json


class DingTalkChannel:
    """DingTalk custom robot webhook adapter."""

    name = "dingtalk"

    def __init__(self, webhook_url: str, *, timeout: float = 10):
        self.webhook_url, self.timeout = webhook_url, timeout

    async def handle_update(self, payload: Any) -> ChannelMessage | None:
        if not isinstance(payload, dict): return None
        text = payload.get("text") or payload.get("content")
        if not isinstance(text, str) or not text.strip(): return None
        return ChannelMessage(self.name, str(payload.get("conversationId", payload.get("chat_id", ""))), str(payload.get("senderStaffId", payload.get("sender_id", ""))), text, str(payload.get("msgId", "")), payload)

    async def send_text(self, chat_id: str, text: str, *, reply_to: str | None = None) -> None:
        await post_json(self.webhook_url, {"msgtype": "text", "text": {"content": text}}, timeout=self.timeout)
