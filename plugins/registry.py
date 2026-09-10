from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import Iterable

from aios_app.plugins.base import AIOSPlugin

logger = logging.getLogger("aios.plugins")


class PluginRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, AIOSPlugin] = {}

    def register(self, plugin: AIOSPlugin) -> None:
        if not plugin.plugin_id or plugin.plugin_id == "unknown":
            raise ValueError("plugins require a stable plugin_id")
        if plugin.plugin_id in self._plugins:
            raise ValueError(f"duplicate plugin_id: {plugin.plugin_id}")
        self._plugins[plugin.plugin_id] = plugin

    def all(self) -> tuple[AIOSPlugin, ...]:
        return tuple(self._plugins.values())

    def enabled(self) -> tuple[AIOSPlugin, ...]:
        return tuple(plugin for plugin in self._plugins.values() if plugin.enabled)

    def discover(self, package_names: Iterable[str] = ("aios_app.plugins.builtin", "aios_app.plugins.providers")) -> None:
        for package_name in package_names:
            try:
                package = importlib.import_module(package_name)
            except ModuleNotFoundError:
                continue
            package_path = getattr(package, "__path__", None)
            if package_path is None:
                continue
            for module_info in pkgutil.iter_modules(package_path, package.__name__ + "."):
                try:
                    module = importlib.import_module(module_info.name)
                    plugin = getattr(module, "PLUGIN", None)
                    if isinstance(plugin, AIOSPlugin):
                        self.register(plugin)
                except Exception:
                    logger.exception("Failed to discover plugin module %s", module_info.name)


def build_default_registry() -> PluginRegistry:
    registry = PluginRegistry()
    registry.discover()
    return registry
