from __future__ import annotations

import asyncio
import json
from urllib import request
from typing import Any

from cat_assistant.channels.base import ChannelMessage


class OneBotV11Channel:
    """QQ adapter for OneBot v11 HTTP reverse events (no SDK required).

    Configure a OneBot instance's ``http://.../send_msg`` API endpoint and
    point its reverse HTTP event to your host. Group and private messages are
    both supported; bot messages and non-text events are ignored.
    """

    name = "qq-onebot"

    def __init__(self, api_url: str, *, access_token: str | None = None, timeout: float = 10):
        self.api_url = api_url.rstrip("/")
        self.access_token = access_token
        self.timeout = timeout

    async def handle_update(self, payload: Any) -> ChannelMessage | None:
        if not isinstance(payload, dict) or payload.get("post_type") != "message":
            return None
        raw = payload.get("message")
        text = raw if isinstance(raw, str) else "".join(
            str(seg.get("data", {}).get("text", ""))
            for seg in (raw or []) if isinstance(seg, dict) and seg.get("type") == "text"
        )
        if not text.strip():
            return None
        detail = "group" if payload.get("message_type") == "group" else "private"
        group_id = payload.get("group_id")
        chat_id = (f"group:{group_id}" if group_id else str(payload.get("user_id") or ""))
        return ChannelMessage(self.name, chat_id, str(payload.get("user_id", chat_id)), text,
                              str(payload.get("message_id", "")), {"message_type": detail})

    async def send_text(self, chat_id: str, text: str, *, reply_to: str | None = None) -> None:
        message_type = "group" if chat_id.startswith("group:") else "private"
        target = chat_id.removeprefix("group:")
        params: dict[str, Any] = {"message_type": message_type, "message": text}
        params["group_id" if message_type == "group" else "user_id"] = int(target)
        if reply_to:
            params["message"] = [{"type": "reply", "data": {"id": str(reply_to)}},
                                  {"type": "text", "data": {"text": text}}]
        url = self.api_url + "/send_msg"
        headers = {"Content-Type": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        body = json.dumps(params, ensure_ascii=False).encode()
        def post() -> None:
            req = request.Request(url, data=body, headers=headers, method="POST")
            with request.urlopen(req, timeout=self.timeout) as response:
                if response.status >= 300:
                    raise RuntimeError(f"OneBot returned HTTP {response.status}")
        await asyncio.to_thread(post)
