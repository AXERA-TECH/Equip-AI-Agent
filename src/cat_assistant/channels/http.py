from __future__ import annotations

import asyncio
import json
from urllib import request
from typing import Any


async def post_json(url: str, payload: dict[str, Any], *, headers: dict[str, str] | None = None, timeout: float = 10) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode()
    request_headers = {"Content-Type": "application/json", **(headers or {})}

    def post() -> dict[str, Any]:
        req = request.Request(url, data=body, headers=request_headers, method="POST")
        with request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            if response.status >= 300:
                raise RuntimeError(f"HTTP {response.status}: {raw[:200]}")
            try:
                return json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return {}

    return await asyncio.to_thread(post)
