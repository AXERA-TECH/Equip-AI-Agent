from __future__ import annotations

import importlib
import inspect
from dataclasses import dataclass, field
from importlib import metadata
from types import ModuleType
from typing import Any, Protocol

from cat_assistant.application.capabilities import CapabilityRegistry
from cat_assistant.application.events import EventRecorder
from cat_assistant.application.tools import ToolRegistry


class ServiceRegistry:
    """Named service definitions used by plugins without importing providers."""

    def __init__(self) -> None:
        self._services: dict[str, Any] = {}

    def register(self, name: str, service: Any, *, replace: bool = False) -> None:
        if name in self._services and not replace:
            raise ValueError(f"duplicate service: {name}")
        self._services[name] = service

    def require(self, name: str) -> Any:
        try:
            return self._services[name]
        except KeyError as exc:
            raise LookupError(f"service is not registered: {name}") from exc

    def get(self, name: str, default: Any = None) -> Any:
        return self._services.get(name, default)

    def unregister(self, name: str) -> None:
        self._services.pop(name, None)


@dataclass(frozen=True, slots=True)
class PluginManifest:
    name: str
    version: str
    description: str = ""
    dependencies: tuple[str, ...] = ()


@dataclass(slots=True)
class PluginContext:
    tools: ToolRegistry
    services: ServiceRegistry
    events: EventRecorder
    capabilities: CapabilityRegistry | None = None
    config: dict[str, Any] = field(default_factory=dict)


class Plugin(Protocol):
    manifest: PluginManifest

    def register(self, context: PluginContext) -> Any: ...

    def shutdown(self, context: PluginContext) -> Any: ...


class PluginManager:
    """Loads, tracks and unloads capability plugins as one runtime boundary."""

    def __init__(self, context: PluginContext) -> None:
        self.context = context
        self._plugins: dict[str, Plugin] = {}

    @property
    def plugins(self) -> tuple[PluginManifest, ...]:
        return tuple(plugin.manifest for plugin in self._plugins.values())

    def _check_load(self, plugin: Plugin) -> PluginManifest:
        manifest = plugin.manifest
        if manifest.name in self._plugins:
            raise ValueError(f"plugin already loaded: {manifest.name}")
        missing = [name for name in manifest.dependencies if name not in self._plugins]
        if missing:
            raise RuntimeError(
                f"plugin {manifest.name} has missing dependencies: {', '.join(missing)}"
            )
        return manifest

    def load_sync(self, plugin: Plugin) -> None:
        """Load a synchronous plugin from a non-async composition root."""
        manifest = self._check_load(plugin)
        try:
            result = plugin.register(self.context)
        except Exception:
            self.context.tools.unregister_owner(manifest.name)
            if self.context.capabilities is not None:
                self.context.capabilities.unregister_owner(manifest.name)
            raise
        if inspect.isawaitable(result):
            self.context.tools.unregister_owner(manifest.name)
            if self.context.capabilities is not None:
                self.context.capabilities.unregister_owner(manifest.name)
            raise TypeError(f"plugin {manifest.name} requires async loading")
        self._plugins[manifest.name] = plugin

    async def load(self, plugin: Plugin) -> None:
        manifest = self._check_load(plugin)
        try:
            result = plugin.register(self.context)
        except Exception:
            self.context.tools.unregister_owner(manifest.name)
            if self.context.capabilities is not None:
                await self.context.capabilities.shutdown_owner(manifest.name)
                self.context.capabilities.unregister_owner(manifest.name)
            raise
        if inspect.isawaitable(result):
            try:
                await result
            except Exception:
                self.context.tools.unregister_owner(manifest.name)
                if self.context.capabilities is not None:
                    await self.context.capabilities.shutdown_owner(manifest.name)
                    self.context.capabilities.unregister_owner(manifest.name)
                raise
        self._plugins[manifest.name] = plugin
        await self.context.events.record(
            "plugin/loaded",
            "runtime",
            plugin=manifest.name,
            version=manifest.version,
        )

    async def unload(self, name: str) -> None:
        plugin = self._plugins.get(name)
        if plugin is None:
            return
        dependents = tuple(
            loaded.manifest.name
            for loaded in self._plugins.values()
            if name in loaded.manifest.dependencies
        )
        if dependents:
            raise RuntimeError(
                f"cannot unload {name}; dependents are still loaded: {', '.join(dependents)}"
            )
        result = plugin.shutdown(self.context)
        plugin_error: BaseException | None = None
        try:
            if inspect.isawaitable(result):
                await result
        except BaseException as exc:
            plugin_error = exc
        capability_error: BaseException | None = None
        if self.context.capabilities is not None:
            try:
                await self.context.capabilities.shutdown_owner(name)
            except BaseException as exc:
                capability_error = exc
        try:
            self.context.tools.unregister_owner(name)
            if self.context.capabilities is not None:
                self.context.capabilities.unregister_owner(name)
            self._plugins.pop(name)
        finally:
            if plugin_error is not None:
                raise plugin_error
            if capability_error is not None:
                raise capability_error
        await self.context.events.record("plugin/unloaded", "runtime", plugin=name)

    async def shutdown(self) -> None:
        first_error: BaseException | None = None
        for name in reversed(tuple(self._plugins)):
            try:
                await self.unload(name)
            except BaseException as exc:
                first_error = first_error or exc
        if first_error is not None:
            raise first_error

    async def load_entry_points(self, group: str = "cat_assistant.plugins") -> None:
        """Load installed plugins explicitly; production should apply an allowlist."""
        entries = metadata.entry_points()
        selected = entries.select(group=group) if hasattr(entries, "select") else entries.get(group, ())
        for entry in selected:
            loaded = entry.load()
            plugin = loaded() if inspect.isclass(loaded) or callable(loaded) else loaded
            await self.load(plugin)

    @staticmethod
    def load_object(reference: str, module: ModuleType | None = None) -> Plugin:
        """Resolve ``package.module:factory`` for config-driven plugin loading."""
        module_name, separator, attribute = reference.partition(":")
        if not separator or not attribute:
            raise ValueError("plugin reference must look like package.module:factory")
        target = module if module is not None else importlib.import_module(module_name)
        loaded = getattr(target, attribute)
        plugin = loaded() if inspect.isclass(loaded) or callable(loaded) else loaded
        return plugin
