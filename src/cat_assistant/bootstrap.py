from __future__ import annotations

from pathlib import Path
import os

from cat_assistant.adapters.runtime_config import RuntimeConfigStore
from cat_assistant.adapters.events import JsonlEventStore
from cat_assistant.adapters.knowledge import InMemoryKnowledge, demo_documents
from cat_assistant.adapters.memory import JsonlMemoryStore, JsonlSessionStore
from cat_assistant.adapters.model import DemoRuleBasedModel, OpenAICompatibleModel
from cat_assistant.adapters.capabilities import builtin_capability_providers
from cat_assistant.adapters.control import SimulatedMachineControl
from cat_assistant.adapters.telemetry import InMemoryTelemetry, demo_snapshots
from cat_assistant.adapters.tracing import LangfuseTracer, TracedLanguageModel
from cat_assistant.adapters.simulation import ScenarioTelemetry, get_simulation_scenario
from cat_assistant.application.events import EventRecorder
from cat_assistant.application.context import ContextBuilder
from cat_assistant.application.capabilities import CapabilityRegistry
from cat_assistant.application.loop import AgentLoop
from cat_assistant.application.plugins import PluginContext, PluginManager, ServiceRegistry
from cat_assistant.application.query import DeterministicQueryService
from cat_assistant.application.runner import BoundedAgentRunner
from cat_assistant.application.orchestration import (
    PlanExecutor,
    PolicyEngine,
    RuleBasedTaskPlanner,
    TaskOrchestrator,
    ToolCallingTaskPlanner,
)
from cat_assistant.application.tools import ToolRegistry
from cat_assistant.application.tracing import NoOpTracer
from cat_assistant.adapters.plugins import ReadOnlyEquipmentToolsPlugin
from cat_assistant.domain.ports import Tracer


def build_demo_app(
    event_path: Path = Path("runtime/events.jsonl"),
    *,
    config_path: Path | None = None,
    simulation_scenario: str | None = None,
    simulation_phase: int = 0,
    tracer: Tracer | None = None,
) -> AgentLoop:
    scenario = get_simulation_scenario(simulation_scenario) if simulation_scenario else None
    if scenario:
        telemetry = ScenarioTelemetry(scenario, phase=simulation_phase)
        knowledge = InMemoryKnowledge(scenario.documents)
        # A scenario is a scripted read-only timeline; control stays unbound so
        # machine.control safely denies rather than fighting the replay.
        machine_control = None
    else:
        telemetry = InMemoryTelemetry(demo_snapshots())
        knowledge = InMemoryKnowledge(demo_documents())
        # Bind a virtual Safety Gateway so the propose -> approve -> execute
        # chain completes end to end against the (already simulated) telemetry.
        machine_control = SimulatedMachineControl(telemetry)
    events = EventRecorder(JsonlEventStore(event_path))
    session_store = JsonlSessionStore(event_path.with_name("sessions.jsonl"))
    memory_store = JsonlMemoryStore(event_path.with_name("memory.jsonl"))
    tools = ToolRegistry()
    capabilities = CapabilityRegistry()
    services = ServiceRegistry()
    services.register("telemetry", telemetry)
    services.register("knowledge", knowledge)
    services.register("sessions", session_store)
    services.register("memory", memory_store)
    plugin_manager = PluginManager(
        PluginContext(tools, services, events, capabilities=capabilities)
    )
    # The synchronous factory keeps bootstrap safe when called from an already
    # running CLI/event loop. Async plugin hosts can use PluginManager.load().
    plugin_manager.load_sync(ReadOnlyEquipmentToolsPlugin())
    model_config = RuntimeConfigStore(config_path).snapshot()["model"] if config_path else None
    base_model = _model_from_config(model_config) if model_config else DemoRuleBasedModel()
    # Wrap the model so every LLM call is reported as a Langfuse generation on
    # the current trace node. With no active trace the decorator just delegates.
    model = TracedLanguageModel(base_model)
    tracer = tracer if tracer is not None else _tracer_from_env()
    max_steps = int(model_config["max_steps"]) if model_config else 4
    model_timeout = float(model_config.get("model_call_timeout_seconds", 60.0)) if model_config else 60.0
    tool_timeout = float(model_config.get("tool_call_timeout_seconds", 30.0)) if model_config else 30.0
    turn_timeout = float(model_config.get("turn_timeout_seconds", 120.0)) if model_config else 120.0
    runner = BoundedAgentRunner(
        model,
        tools,
        events,
        max_steps=max_steps,
        model_call_timeout_seconds=model_timeout,
        tool_call_timeout_seconds=tool_timeout,
    )
    context_builder = ContextBuilder(
        session_store,
        memory_store,
        history_limit=8,
        max_chars=12_000,
    )
    query_service = DeterministicQueryService()
    for provider in builtin_capability_providers(
        knowledge=knowledge,
        query_service=query_service,
        agent_runner=runner,
        context_builder=context_builder,
        machine_control=machine_control,
        # The diagnostic agent runs the (slow) model synthesis. Bound its
        # capability-level timeout by the configured turn budget rather than a
        # hardcoded default, so operators who raise the timeouts actually get the
        # longer wait instead of a hidden ~90s ceiling.
        diagnostic_timeout_seconds=int(turn_timeout),
    ):
        capabilities.register(provider, owner="cat.core")
    rule_planner = RuleBasedTaskPlanner(capabilities)
    planning_mode = (
        str(model_config.get("planning_mode", "auto")) if model_config else "auto"
    )
    use_tool_calls = planning_mode == "tool_call" or (
        planning_mode == "auto"
        and model_config is not None
        and model_config["provider"] != "demo"
    )
    planner = (
        ToolCallingTaskPlanner(
            model,
            capabilities,
            rule_planner,
            # The planner no longer caps how many capabilities the LLM may chain;
            # BoundedAgentRunner (max_steps above) still bounds the inner agent loop.
            max_steps=None,
            model_call_timeout_seconds=model_timeout,
            events=events,
        )
        if use_tool_calls
        else rule_planner
    )
    orchestrator = TaskOrchestrator(
        planner,
        PlanExecutor(capabilities, PolicyEngine(), events),
    )
    app = AgentLoop(
        telemetry=telemetry,
        orchestrator=orchestrator,
        events=events,
        session_store=session_store,
        memory=memory_store,
        turn_timeout_seconds=turn_timeout,
        tracer=tracer,
    )
    app.plugin_manager = plugin_manager
    app.capability_registry = capabilities
    app.runner = runner
    app.tool_registry = tools
    return app


def _model_from_config(config: dict[str, object]):
    provider = str(config.get("provider", "demo"))
    if provider == "demo":
        return DemoRuleBasedModel()
    defaults = {
        "vllm": "http://127.0.0.1:8000/v1",
        "ollama": "http://127.0.0.1:11434/v1",
    }
    base_url = str(config.get("base_url", "") or defaults.get(provider, ""))
    if not base_url:
        raise ValueError("a non-demo model requires base_url")
    api_key = str(config.get("api_key", ""))
    api_key_env = str(config.get("api_key_env", ""))
    if api_key_env:
        api_key = os.environ.get(api_key_env, "")
    raw_stop = config.get("stop")
    stop = tuple(str(item) for item in raw_stop) if isinstance(raw_stop, (list, tuple)) else ()
    return OpenAICompatibleModel(
        model=str(config.get("model", "")),
        base_url=base_url,
        api_key=api_key,
        temperature=float(config.get("temperature", 0.1)),
        max_tokens=int(config.get("max_tokens", 1024)),
        timeout_seconds=int(config.get("timeout_seconds", 30)),
        disable_thinking=bool(config.get("disable_thinking", False)),
        stop=stop,
    )


def _tracer_from_env() -> Tracer:
    """Enable Langfuse tracing when its standard env credentials are present.

    Kept opt-in and best-effort: with no keys (or if the optional ``langfuse``
    package is missing) the assistant runs with a no-op tracer and zero extra
    dependencies. A misconfiguration warns to stderr rather than crashing a
    safety-relevant read-only assistant.
    """
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY")
    if not (public_key and secret_key):
        return NoOpTracer()
    # v4 prefers LANGFUSE_BASE_URL; LANGFUSE_HOST is the deprecated alias. Pass
    # None when neither is set so the SDK applies its own default (EU cloud).
    host = os.environ.get("LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_HOST")
    try:
        return LangfuseTracer(
            public_key=public_key,
            secret_key=secret_key,
            host=host,
        )
    except Exception as exc:  # missing package or client init failure
        import sys

        print(
            f"[equip] Langfuse tracing disabled: {exc}",
            file=sys.stderr,
        )
        return NoOpTracer()


def load_env_file(path: Path | None = None) -> Path | None:
    """Best-effort load of ``KEY=VALUE`` lines from a local ``.env`` into the
    process environment, so secrets need not be re-exported each shell.

    Search order (first readable file wins): the explicit ``path``, else
    ``$CAT_ENV_FILE``, then ``./.env``, then ``./config/.env`` (next to the
    committed ``config/.env.example`` template). A variable already present in
    the real environment always wins (the file never overrides it), empty values
    and missing files are ignored, and ``export`` prefixes / surrounding quotes
    are tolerated. Called only from the CLI/UI entry points, so tests and library
    embedders never pick up a stray ``.env``. Returns the file applied, or None.
    Keep these files gitignored.
    """
    if path is not None:
        candidates = [path]
    else:
        candidates = [
            Path(name)
            for name in (os.environ.get("CAT_ENV_FILE"), ".env", "config/.env")
            if name
        ]
    for candidate in candidates:
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.removeprefix("export ").strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key and value and key not in os.environ:
                os.environ[key] = value
        return candidate
    return None
