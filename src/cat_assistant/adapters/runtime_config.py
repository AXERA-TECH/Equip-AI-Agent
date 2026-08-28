"""Local configuration persistence used by the dependency-free debug console.

The store intentionally persists declarations, not live runtime objects.  A saved
MCP server or plugin is therefore "configured" until the production host has
validated and loaded it.  Secrets are referenced by environment-variable name
and are never accepted as config values.
"""

from __future__ import annotations

import json
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4


DEFAULT_RUNTIME_CONFIG: dict[str, Any] = {
    "version": 1,
    "model": {
        "provider": "demo",
        "model": "demo-rule-based",
        "base_url": "",
        "api_key": "",
        "api_key_env": "",
        "temperature": 0.1,
        "max_tokens": 1024,
        "timeout_seconds": 30,
        "max_steps": 4,
        "planning_mode": "auto",
        "disable_thinking": False,
        "stop": [],
        "model_call_timeout_seconds": 60.0,
        "tool_call_timeout_seconds": 30.0,
        "turn_timeout_seconds": 120.0,
    },
    "mcp_servers": [],
    "plugins": [],
}


class RuntimeConfigStore:
    """Small atomic JSON store for local-only UI configuration."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return deepcopy(DEFAULT_RUNTIME_CONFIG)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return deepcopy(DEFAULT_RUNTIME_CONFIG)
        if not isinstance(raw, dict):
            return deepcopy(DEFAULT_RUNTIME_CONFIG)
        data = deepcopy(DEFAULT_RUNTIME_CONFIG)
        if isinstance(raw.get("model"), dict):
            data["model"].update(raw["model"])
        for key in ("mcp_servers", "plugins"):
            if isinstance(raw.get(key), list):
                data[key] = [item for item in raw[key] if isinstance(item, dict)]
        return data

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._data)

    def save_model(self, payload: dict[str, Any]) -> dict[str, Any]:
        provider = self._required(payload, "provider", 40)
        if provider not in {"demo", "vllm", "ollama", "openai_compatible", "axllm"}:
            raise ValueError("unsupported model provider")
        model = self._required(payload, "model", 200)
        base_url = self._optional(payload, "base_url", 500)
        api_key = self._optional(payload, "api_key", 500)
        api_key_env = self._optional(payload, "api_key_env", 200)
        config = {
            "provider": provider,
            "model": model,
            "base_url": base_url,
            "api_key": api_key,
            "api_key_env": api_key_env,
            "temperature": self._number(payload, "temperature", 0.0, 2.0),
            "max_tokens": self._integer(payload, "max_tokens", 1, 131_072),
            "timeout_seconds": self._integer(payload, "timeout_seconds", 1, 600),
            "max_steps": self._integer(payload, "max_steps", 1, 12, default=4),
            "planning_mode": self._planning_mode(payload.get("planning_mode", "auto")),
            "disable_thinking": bool(payload.get("disable_thinking", False)),
            "stop": self._string_list(payload.get("stop"), "stop"),
            "model_call_timeout_seconds": self._number(
                payload, "model_call_timeout_seconds", 0.1, 600.0, default=60.0
            ),
            "tool_call_timeout_seconds": self._number(
                payload, "tool_call_timeout_seconds", 0.1, 600.0, default=30.0
            ),
            "turn_timeout_seconds": self._number(
                payload, "turn_timeout_seconds", 0.1, 1800.0, default=120.0
            ),
        }
        with self._lock:
            self._data["model"] = config
            self._write()
            return deepcopy(config)

    def upsert_mcp(self, payload: dict[str, Any]) -> dict[str, Any]:
        item_id = self._identifier(payload.get("id") or f"mcp-{uuid4()}", "MCP id")
        transport = self._required(payload, "transport", 30)
        if transport not in {"streamable_http", "sse", "stdio"}:
            raise ValueError("unsupported MCP transport")
        endpoint = self._optional(payload, "endpoint", 1000)
        command = self._optional(payload, "command", 500)
        if transport == "stdio" and not command:
            raise ValueError("stdio MCP requires command")
        if transport != "stdio" and not endpoint:
            raise ValueError("remote MCP requires endpoint")
        item = {
            "id": item_id,
            "name": self._required(payload, "name", 100),
            "transport": transport,
            "endpoint": endpoint,
            "command": command,
            "arguments": self._string_list(payload.get("arguments"), "arguments"),
            "env_keys": self._string_list(payload.get("env_keys"), "env_keys"),
            "tool_allowlist": self._string_list(
                payload.get("tool_allowlist") or ["*"], "tool_allowlist"
            ),
            "enabled": bool(payload.get("enabled", False)),
            "status": "configured",
        }
        return self._upsert("mcp_servers", item)

    def delete_mcp(self, item_id: str) -> None:
        self._delete("mcp_servers", item_id)

    def upsert_plugin(self, payload: dict[str, Any]) -> dict[str, Any]:
        item_id = self._identifier(
            payload.get("id") or f"plugin-{uuid4()}", "plugin id"
        )
        reference = self._required(payload, "reference", 300)
        if ":" not in reference:
            raise ValueError("plugin reference must look like package.module:factory")
        config = payload.get("config", {})
        if not isinstance(config, dict):
            raise ValueError("plugin config must be a JSON object")
        item = {
            "id": item_id,
            "name": self._required(payload, "name", 100),
            "reference": reference,
            "version": self._optional(payload, "version", 60),
            "config": config,
            "enabled": bool(payload.get("enabled", False)),
            "status": "configured",
        }
        return self._upsert("plugins", item)

    def delete_plugin(self, item_id: str) -> None:
        self._delete("plugins", item_id)

    def _upsert(self, collection: str, item: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            rows = self._data[collection]
            for index, current in enumerate(rows):
                if current.get("id") == item["id"]:
                    rows[index] = item
                    break
            else:
                rows.append(item)
            self._write()
            return deepcopy(item)

    def _delete(self, collection: str, item_id: str) -> None:
        safe_id = self._identifier(item_id, "id")
        with self._lock:
            self._data[collection] = [
                row for row in self._data[collection] if row.get("id") != safe_id
            ]
            self._write()

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)

    @staticmethod
    def _required(payload: dict[str, Any], key: str, limit: int) -> str:
        value = str(payload.get(key, "")).strip()
        if not value or len(value) > limit:
            raise ValueError(f"{key} must contain 1-{limit} characters")
        return value

    @staticmethod
    def _optional(payload: dict[str, Any], key: str, limit: int) -> str:
        value = str(payload.get(key, "")).strip()
        if len(value) > limit:
            raise ValueError(f"{key} must contain at most {limit} characters")
        return value

    @staticmethod
    def _identifier(value: Any, label: str) -> str:
        normalized = str(value or "").strip().lower().replace(" ", "-")
        if not normalized or len(normalized) > 100:
            raise ValueError(f"{label} is invalid")
        if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for character in normalized):
            raise ValueError(f"{label} may only contain letters, numbers, dot, dash and underscore")
        return normalized

    @staticmethod
    def _number(
        payload: dict[str, Any],
        key: str,
        minimum: float,
        maximum: float,
        *,
        default: float | None = None,
    ) -> float:
        try:
            value = float(payload.get(key, default))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be a number") from exc
        if not minimum <= value <= maximum:
            raise ValueError(f"{key} must be between {minimum} and {maximum}")
        return value

    @staticmethod
    def _integer(
        payload: dict[str, Any],
        key: str,
        minimum: int,
        maximum: int,
        *,
        default: int | None = None,
    ) -> int:
        try:
            value = int(payload.get(key, default))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be an integer") from exc
        if not minimum <= value <= maximum:
            raise ValueError(f"{key} must be between {minimum} and {maximum}")
        return value

    @staticmethod
    def _string_list(value: Any, label: str) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > 200:
            raise ValueError(f"{label} must be a list")
        result = [str(item).strip() for item in value if str(item).strip()]
        if any(len(item) > 500 for item in result):
            raise ValueError(f"{label} contains an item that is too long")
        return result

    @staticmethod
    def _planning_mode(value: Any) -> str:
        mode = str(value).strip()
        if mode not in {"auto", "tool_call", "rule"}:
            raise ValueError("planning_mode must be auto, tool_call or rule")
        return mode
