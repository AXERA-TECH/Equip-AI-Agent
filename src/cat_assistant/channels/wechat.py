from __future__ import annotations

import asyncio
import json
from urllib import request
from typing import Any

from cat_assistant.channels.base import ChannelMessage


class WeChatWebhookChannel:
    """微信 webhook adapter (企业微信机器人/自建网关 compatible).

    Incoming gateways should POST ``{"chat_id", "sender_id", "text"}`` (the
    common fields used by nanobot-style channel bridges).  Outbound delivery is
    the standard 企业微信 bot webhook format.  A custom sender can be injected
    for personal-WeChat/iLink gateways.
    """

    name = "wechat"

    def __init__(self, webhook_url: str, *, sender=None, timeout: float = 10):
        self.webhook_url = webhook_url
        self.sender = sender
        self.timeout = timeout

    async def handle_update(self, payload: Any) -> ChannelMessage | None:
        if not isinstance(payload, dict):
            return None
        text = payload.get("text", payload.get("content", ""))
        if not isinstance(text, str) or not text.strip():
            return None
        chat_id = str(payload.get("chat_id") or payload.get("conversation_id") or payload.get("from_user", ""))
        sender = str(payload.get("sender_id") or payload.get("from_user") or chat_id)
        if not chat_id:
            return None
        return ChannelMessage(self.name, chat_id, sender, text, str(payload.get("message_id", "")), payload)

    async def send_text(self, chat_id: str, text: str, *, reply_to: str | None = None) -> None:
        if self.sender is not None:
            await self.sender(chat_id, text, reply_to=reply_to)
            return
        body = json.dumps({"msgtype": "text", "text": {"content": text}}, ensure_ascii=False).encode()
        def post() -> None:
            req = request.Request(self.webhook_url, data=body, headers={"Content-Type": "application/json"}, method="POST")
            with request.urlopen(req, timeout=self.timeout) as response:
                if response.status >= 300:
                    raise RuntimeError(f"WeChat returned HTTP {response.status}")
        await asyncio.to_thread(post)
