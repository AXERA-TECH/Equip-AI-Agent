"""Local dependency-free debug UI for the Cat assistant."""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from cat_assistant.adapters.runtime_config import RuntimeConfigStore
from cat_assistant.application.loop import AgentLoop
from cat_assistant.bootstrap import build_demo_app, load_env_file
from cat_assistant.adapters.simulation import get_simulation_scenario, simulation_catalog
from cat_assistant.domain.models import MemoryRecord, Utterance


UI_ROOT = Path(__file__).with_name("ui")


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


class DebugRuntime:
    def __init__(
        self,
        event_path: Path,
        config_path: Path | None = None,
        *,
        simulation_scenario: str | None = None,
        simulation_phase: int = 0,
    ) -> None:
        self.event_path = event_path
        self.config_path = config_path or event_path.with_name("runtime-config.json")
        self.simulation_scenario = simulation_scenario
        self.simulation_phase = simulation_phase
        self.config_store = RuntimeConfigStore(self.config_path)
        self.app = build_demo_app(
            event_path,
            config_path=self.config_path,
            simulation_scenario=simulation_scenario,
            simulation_phase=simulation_phase,
        )
        self._loop = asyncio.new_event_loop()
        self._call_lock = threading.Lock()

    def call(self, make_coroutine: Callable[[], Any]) -> Any:
        # ThreadingHTTPServer may invoke handlers on different threads. Run all
        # async application work on one shared loop, serialised by this lock, so
        # session locks and store locks never cross event loops. The coroutine
        # is built inside the lock so every call observes the current ``self.app``
        # even across a hot model-config reload that swaps it.
        with self._call_lock:
            return self._loop.run_until_complete(make_coroutine())

    def close(self) -> None:
        try:
            self.call(lambda: self.app.shutdown())
        finally:
            self._loop.close()

    def events(self, limit: int = 120) -> list[dict[str, Any]]:
        if not self.event_path.exists():
            return []
        records: list[dict[str, Any]] = []
        try:
            with self.event_path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        value = json.loads(line)
                        if isinstance(value, dict):
                            records.append(value)
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return []
        return records[-max(1, min(limit, 500)) :]

    def state(
        self,
        machine_id: str,
        session_id: str,
        operator_id: str = "debug-operator",
    ) -> dict[str, Any]:
        machine = self.call(lambda: self.app.get_machine_snapshot(machine_id))
        turns = self.call(
            lambda: self.app.get_recent_turns(
                session_id,
                machine_id=machine_id,
                operator_id=operator_id,
                limit=30,
            )
        )
        manifests = self.app.plugin_manager.plugins if self.app.plugin_manager else ()
        simulation = None
        if self.simulation_scenario:
            scenario = get_simulation_scenario(self.simulation_scenario)
            sample = scenario.sample(self.simulation_phase)
            simulation = {
                "scenario_id": scenario.scenario_id,
                "title": scenario.title,
                "description": scenario.description,
                "phase": self.simulation_phase,
                "phase_count": len(scenario.samples),
                "sample": _jsonable(sample),
            }
        return {
            "machine": _jsonable(machine),
            "tools": _jsonable(self.app.tool_specs),
            "capabilities": _jsonable(self.app.capability_inventory),
            "plugins": _jsonable(manifests),
            "mcp_servers": self.config_store.snapshot()["mcp_servers"],
            "skills": [],
            "configuration": self.configuration(),
            "session_id": session_id,
            "events": self.events(),
            "turns": _jsonable(turns),
            "simulation": simulation,
        }

    def simulations(self) -> list[dict[str, Any]]:
        return [
            {
                "scenario_id": scenario.scenario_id,
                "title": scenario.title,
                "description": scenario.description,
                "phase_count": len(scenario.samples),
                "phases": [
                    {"phase": index, "name": sample.phase, "operator_note": sample.operator_note}
                    for index, sample in enumerate(scenario.samples)
                ],
            }
            for scenario in simulation_catalog().values()
        ]

    def machines(self) -> list[dict[str, Any]]:
        snapshots = self.call(lambda: self.app.list_machines())
        return [_jsonable(snapshot) for snapshot in snapshots]

    def turn(
        self,
        *,
        text: str,
        session_id: str,
        machine_id: str,
        operator_id: str,
    ) -> dict[str, Any]:
        response = self.call(
            lambda: self.app.handle(
                Utterance(
                    text=text,
                    session_id=session_id,
                    machine_id=machine_id,
                    operator_id=operator_id,
                )
            )
        )
        return {
            "response": _jsonable(response),
            "state": self.state(machine_id, session_id, operator_id),
        }

    def delete_session(self, session_id: str) -> dict[str, Any]:
        """Delete a session's backend history so the UI can stay in sync."""
        session_id = session_id.strip()
        if not session_id:
            raise ValueError("session_id is required")
        removed = self.call(lambda: self.app.clear_session(session_id))
        return {"ok": True, "session_id": session_id, "removed_turns": removed}

    def remember(self, payload: dict[str, Any]) -> dict[str, Any]:
        memory = MemoryRecord(
            content=str(payload.get("content", "")).strip(),
            kind=str(payload.get("kind", "fact")),
            source="debug-ui",
            machine_id=payload.get("machine_id") or None,
            operator_id=payload.get("operator_id") or None,
            confidence=float(payload.get("confidence", 1.0)),
        )
        self.call(lambda: self.app.remember(memory))
        return {"ok": True, "memory": _jsonable(memory)}

    def configuration(self) -> dict[str, Any]:
        config = self.config_store.snapshot()
        adapter_name = self.app.model_adapter_name
        config["runtime"] = {
            "model_adapter": adapter_name,
            "model_config_applied": (
                config["model"]["provider"] == "demo"
                or adapter_name == "OpenAICompatibleModel"
            ),
            "mcp_host_available": False,
            "plugin_hot_load_available": False,
        }
        return config

    def save_model_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        # Apply the new model without a process restart: persist, then rebuild the
        # app from the persisted config and swap it in. If the rebuild/startup
        # fails (e.g. an invalid provider config), restore the last good config
        # and keep the current app serving, so a bad save neither takes the
        # console down nor leaves an unbuildable file for the next restart.
        previous = self.config_store.snapshot()["model"]
        model = self.config_store.save_model(payload)
        try:
            self._reload_app()
        except Exception:
            self.config_store.save_model(previous)
            raise
        return {
            "ok": True,
            "model": model,
            "model_adapter": self.app.model_adapter_name,
        }

    def _reload_app(self) -> None:
        # Build the replacement outside the loop lock (pure construction, no
        # network) so in-flight turns are not blocked while it is assembled.
        new_app = build_demo_app(
            self.event_path,
            config_path=self.config_path,
            simulation_scenario=self.simulation_scenario,
            simulation_phase=self.simulation_phase,
        )
        self.call(lambda: self._swap_app(new_app))

    async def _swap_app(self, new_app: AgentLoop) -> None:
        # Start the replacement before touching the live reference; if startup
        # fails, tear it back down and keep the previous app in place. Runs under
        # the shared loop lock, so no turn straddles the swap.
        try:
            await new_app.startup()
        except BaseException:
            await new_app.shutdown()
            raise
        previous = self.app
        self.app = new_app
        await previous.shutdown()

    def save_mcp_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "mcp": self.config_store.upsert_mcp(payload)}

    def delete_mcp_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.config_store.delete_mcp(str(payload.get("id", "")))
        return {"ok": True}

    def save_plugin_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "plugin": self.config_store.upsert_plugin(payload)}

    def delete_plugin_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.config_store.delete_plugin(str(payload.get("id", "")))
        return {"ok": True}


class DebugHandler(BaseHTTPRequestHandler):
    runtime: DebugRuntime

    def log_message(self, format: str, *args: Any) -> None:
        # Keep the terminal focused on useful application logs.
        return

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: HTTPStatus, payload: Any) -> None:
        body = json.dumps(_jsonable(payload), ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._json(status, {"error": message})

    def _query(self) -> dict[str, str]:
        parsed = parse_qs(urlparse(self.path).query)
        return {key: values[-1] for key, values in parsed.items() if values}

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self._json(HTTPStatus.OK, {"ok": True})
            return
        if parsed.path == "/api/state":
            query = self._query()
            machine_id = query.get("machine_id", "cat-306-demo")
            session_id = query.get("session_id", "debug-session")
            operator_id = query.get("operator_id", "debug-operator")
            try:
                self._json(
                    HTTPStatus.OK,
                    self.runtime.state(machine_id, session_id, operator_id),
                )
            except Exception as exc:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, type(exc).__name__)
            return
        if parsed.path == "/api/events":
            self._json(HTTPStatus.OK, {"events": self.runtime.events()})
            return
        if parsed.path == "/api/simulations":
            self._json(HTTPStatus.OK, {"simulations": self.runtime.simulations()})
            return
        if parsed.path == "/api/machines":
            self._json(HTTPStatus.OK, {"machines": self.runtime.machines()})
            return
        if parsed.path == "/api/config":
            self._json(HTTPStatus.OK, self.runtime.configuration())
            return

        relative = "index.html" if parsed.path in {"", "/"} else parsed.path.lstrip("/")
        target = (UI_ROOT / relative).resolve()
        if not target.is_file() or not target.is_relative_to(UI_ROOT.resolve()):
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
        }.get(target.suffix, "application/octet-stream")
        try:
            self._send(HTTPStatus.OK, target.read_bytes(), content_type)
        except OSError:
            self._error(HTTPStatus.NOT_FOUND, "not found")

    def _read_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("invalid content length") from exc
        if length < 0 or length > 1_000_000:
            raise ValueError("request body is too large")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def do_POST(self) -> None:  # noqa: N802
        try:
            payload = self._read_json()
            if self.path == "/api/turn":
                text = str(payload.get("text", "")).strip()
                if not text or len(text) > 2_000:
                    raise ValueError("text must contain 1-2000 characters")
                result = self.runtime.turn(
                    text=text,
                    session_id=str(payload.get("session_id", "debug-session")),
                    machine_id=str(payload.get("machine_id", "cat-306-demo")),
                    operator_id=str(payload.get("operator_id", "debug-operator")),
                )
                self._json(HTTPStatus.OK, result)
                return
            if self.path == "/api/remember":
                self._json(HTTPStatus.OK, self.runtime.remember(payload))
                return
            if self.path == "/api/session/delete":
                self._json(
                    HTTPStatus.OK,
                    self.runtime.delete_session(str(payload.get("session_id", ""))),
                )
                return
            if self.path == "/api/config/model":
                self._json(HTTPStatus.OK, self.runtime.save_model_config(payload))
                return
            if self.path == "/api/config/mcp":
                self._json(HTTPStatus.OK, self.runtime.save_mcp_config(payload))
                return
            if self.path == "/api/config/mcp/delete":
                self._json(HTTPStatus.OK, self.runtime.delete_mcp_config(payload))
                return
            if self.path == "/api/config/plugin":
                self._json(HTTPStatus.OK, self.runtime.save_plugin_config(payload))
                return
            if self.path == "/api/config/plugin/delete":
                self._json(HTTPStatus.OK, self.runtime.delete_plugin_config(payload))
                return
            self._error(HTTPStatus.NOT_FOUND, "not found")
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, type(exc).__name__)


def create_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    event_path: Path | None = None,
    config_path: Path | None = None,
    simulation_scenario: str | None = None,
    simulation_phase: int = 0,
):
    runtime = DebugRuntime(
        event_path or Path("runtime/debug-events.jsonl"),
        config_path=config_path,
        simulation_scenario=simulation_scenario,
        simulation_phase=simulation_phase,
    )

    class Handler(DebugHandler):
        pass

    Handler.runtime = runtime
    server = ThreadingHTTPServer((host, port), Handler)
    server.runtime = runtime  # type: ignore[attr-defined]
    return server


def main() -> None:
    load_env_file()
    parser = argparse.ArgumentParser(description="Run the Cat Assistant local debug UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--event-path", type=Path, default=Path("runtime/debug-events.jsonl"))
    parser.add_argument("--config-path", type=Path, default=Path("runtime/runtime-config.json"))
    parser.add_argument(
        "--scenario",
        choices=tuple(simulation_catalog()),
        help="run a deterministic multi-stage equipment simulation",
    )
    parser.add_argument("--phase", type=int, default=0, help="initial simulation phase")
    args = parser.parse_args()
    server = create_server(
        args.host, args.port, args.event_path, args.config_path,
        simulation_scenario=args.scenario, simulation_phase=args.phase,
    )
    print(f"Cat Assistant debug UI: http://{args.host}:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        server.runtime.close()  # type: ignore[attr-defined]


if __name__ == "__main__":
    main()
