#!/usr/bin/env python3
"""Run the Cat assistant's OneBot v11 QQ webhook bridge.

Environment:
  ONEBOT_API_URL   OneBot HTTP API base, default http://127.0.0.1:5700
  ONEBOT_TOKEN     Optional OneBot access token
  CAT_MACHINE_ID   Telemetry machine id, default cat-306-demo
  CAT_CONFIG_PATH  Optional runtime config JSON
  CAT_WEBHOOK_HOST Host to bind, default 127.0.0.1
  CAT_WEBHOOK_PORT Port to bind, default 8088
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from cat_assistant.bootstrap import build_demo_app
from cat_assistant.channels import ChannelBridge, OneBotV11Channel, WebhookServer


async def main() -> None:
    config = os.getenv("CAT_CONFIG_PATH")
    app = build_demo_app(config_path=Path(config) if config else None)
    channel = OneBotV11Channel(
        os.getenv("ONEBOT_API_URL", "http://127.0.0.1:5700"),
        access_token=os.getenv("ONEBOT_TOKEN"),
    )
    server = WebhookServer(ChannelBridge(app, machine_id=os.getenv("CAT_MACHINE_ID", "cat-306-demo")))
    server.route("/qq", channel)
    await server.start(os.getenv("CAT_WEBHOOK_HOST", "127.0.0.1"), int(os.getenv("CAT_WEBHOOK_PORT", "8088")))
    print("QQ webhook listening on /qq")
    try:
        await server.serve_forever()
    finally:
        await server.close()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
