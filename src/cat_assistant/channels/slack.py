from __future__ import annotations

from typing import Any

from cat_assistant.channels.base import ChannelMessage
from cat_assistant.channels.http import post_json


class SlackChannel:
    """Slack Events API + chat.postMessage adapter."""

    name = "slack"

    def __init__(self, bot_token: str, *, timeout: float = 10):
        self.url = "https://slack.com/api/chat.postMessage"
        self.headers = {"Authorization": f"Bearer {bot_token}"}
        self.timeout = timeout

    async def handle_update(self, payload: Any) -> ChannelMessage | None:
        event = payload.get("event") if isinstance(payload, dict) else None
        if not isinstance(event, dict) or event.get("type") != "message" or event.get("subtype") or not event.get("text"):
            return None
        return ChannelMessage(self.name, str(event.get("channel", "")), str(event.get("user", "")), str(event["text"]), str(event.get("ts", "")), event)

    async def send_text(self, chat_id: str, text: str, *, reply_to: str | None = None) -> None:
        payload: dict[str, Any] = {"channel": chat_id, "text": text}
        if reply_to:
            payload["thread_ts"] = reply_to
        await post_json(self.url, payload, headers=self.headers, timeout=self.timeout)
