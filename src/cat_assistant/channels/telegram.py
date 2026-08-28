from __future__ import annotations

from typing import Any

from cat_assistant.channels.base import ChannelMessage
from cat_assistant.channels.http import post_json


class TelegramChannel:
    """Telegram Bot API adapter for webhook updates."""

    name = "telegram"

    def __init__(self, bot_token: str, *, timeout: float = 10):
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.timeout = timeout

    async def handle_update(self, payload: Any) -> ChannelMessage | None:
        msg = payload.get("message") if isinstance(payload, dict) else None
        if not isinstance(msg, dict) or not isinstance(msg.get("text"), str):
            return None
        chat, sender = msg.get("chat", {}), msg.get("from", {})
        return ChannelMessage(self.name, str(chat.get("id", "")), str(sender.get("id", "")), msg["text"], str(msg.get("message_id", "")), msg)

    async def send_text(self, chat_id: str, text: str, *, reply_to: str | None = None) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_to:
            payload["reply_parameters"] = {"message_id": int(reply_to)}
        await post_json(self.base_url + "/sendMessage", payload, timeout=self.timeout)
