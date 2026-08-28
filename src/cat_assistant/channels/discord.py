from __future__ import annotations

from typing import Any

from cat_assistant.channels.base import ChannelMessage
from cat_assistant.channels.http import post_json


class DiscordChannel:
    """Discord interaction/webhook-compatible adapter."""

    name = "discord"

    def __init__(self, webhook_url: str, *, timeout: float = 10):
        self.webhook_url, self.timeout = webhook_url, timeout

    async def handle_update(self, payload: Any) -> ChannelMessage | None:
        if not isinstance(payload, dict) or not isinstance(payload.get("content"), str):
            return None
        author = payload.get("author") or payload.get("member", {}).get("user", {})
        return ChannelMessage(self.name, str(payload.get("channel_id", "")), str(author.get("id", "")), payload["content"], str(payload.get("id", "")), payload)

    async def send_text(self, chat_id: str, text: str, *, reply_to: str | None = None) -> None:
        await post_json(self.webhook_url, {"content": text}, timeout=self.timeout)
