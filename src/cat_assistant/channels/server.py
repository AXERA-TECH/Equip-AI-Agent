from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

from cat_assistant.channels.base import MessageChannel
from cat_assistant.channels.bridge import ChannelBridge


class WebhookServer:
    """Dependency-free JSON webhook host for channel adapters.

    Register ``/qq`` and ``/wechat`` (or any paths) with their channel. The
    server intentionally binds to localhost by default; put authentication and
    TLS in a reverse proxy before exposing it externally.
    """

    def __init__(self, bridge: ChannelBridge, *, max_body_bytes: int = 1_048_576):
        self.bridge = bridge
        self.max_body_bytes = max_body_bytes
        self._routes: dict[str, MessageChannel] = {}
        self._server: asyncio.AbstractServer | None = None

    def route(self, path: str, channel: MessageChannel) -> None:
        if not path.startswith("/"):
            raise ValueError("webhook path must start with '/'")
        self._routes[path] = channel

    async def start(self, host: str = "127.0.0.1", port: int = 8088) -> None:
        self._server = await asyncio.start_server(self._handle, host, port)

    async def serve_forever(self) -> None:
        if self._server is None:
            raise RuntimeError("server has not been started")
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin1").split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            headers = {line.split(":", 1)[0].lower(): line.split(":", 1)[1].strip()
                       for line in lines[1:] if ":" in line}
            length = int(headers.get("content-length", "0"))
            if method != "POST" or length > self.max_body_bytes or path not in self._routes:
                await self._reply(writer, 404 if path not in self._routes else 413, b"{}")
                return
            payload = json.loads(await reader.readexactly(length))
            await self.bridge.dispatch_update(self._routes[path], payload)
            await self._reply(writer, 200, b'{"ok":true}')
        except (ValueError, asyncio.IncompleteReadError, json.JSONDecodeError):
            await self._reply(writer, 400, b'{"ok":false}')
        finally:
            writer.close()
            await writer.wait_closed()

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, status: int, body: bytes) -> None:
        reason = "OK" if status == 200 else "Bad Request"
        writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        await writer.drain()
