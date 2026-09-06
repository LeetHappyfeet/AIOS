from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from aios_app.plugins.registry import PluginRegistry, build_default_registry
from aios_app.plugins.types import PluginContribution, PluginRuntimeContext

logger = logging.getLogger("aios.plugins")


class PluginManager:
    def __init__(self, registry: Optional[PluginRegistry] = None, *, default_timeout: float = 0.25):
        self.registry = registry or build_default_registry()
        self.default_timeout = max(0.01, float(default_timeout))
        self._started = False

    async def startup(self) -> None:
        if self._started:
            return
        for plugin in self.registry.enabled():
            timeout = max(0.01, float(getattr(plugin, "timeout_seconds", self.default_timeout)))
            try:
                await asyncio.wait_for(plugin.startup(), timeout=timeout)
            except Exception:
                logger.exception("HUD plugin %s startup failed", plugin.plugin_id)
        self._started = True

    async def shutdown(self) -> None:
        if not self._started:
            return
        for plugin in self.registry.enabled():
            timeout = max(0.01, float(getattr(plugin, "timeout_seconds", self.default_timeout)))
            try:
                await asyncio.wait_for(plugin.shutdown(), timeout=timeout)
            except Exception:
                logger.exception("HUD plugin %s shutdown failed", plugin.plugin_id)
        self._started = False

    async def collect(self, context: PluginRuntimeContext) -> dict[str, Any]:
        plugins = [plugin for plugin in self.registry.enabled() if plugin.applies_to(context)]
        if not plugins:
            return {
                "plugins": {},
                "sections": [],
                "retrieval_signals": [],
                "actions": [],
                "status": {},
            }

        async def run(plugin):
            timeout = max(0.01, float(getattr(plugin, "timeout_seconds", self.default_timeout)))
            try:
                contribution = await asyncio.wait_for(plugin.collect(context), timeout=timeout)
                if contribution.plugin_id != plugin.plugin_id:
                    raise ValueError(
                        f"plugin {plugin.plugin_id} returned contribution for {contribution.plugin_id}"
                    )
                if contribution.is_stale(datetime.now(timezone.utc)):
                    return plugin.plugin_id, "stale", None
                return plugin.plugin_id, "ok", contribution
            except asyncio.TimeoutError:
                logger.warning("HUD plugin %s timed out after %.3fs", plugin.plugin_id, timeout)
                return plugin.plugin_id, "timeout", None
            except Exception:
                logger.exception("HUD plugin %s failed", plugin.plugin_id)
                return plugin.plugin_id, "error", None

        results = await asyncio.gather(*(run(plugin) for plugin in plugins))

        plugin_payload: dict[str, Any] = {}
        sections: list[dict[str, Any]] = []
        retrieval_signals: list[dict[str, Any]] = []
        actions: list[dict[str, Any]] = []
        status: dict[str, str] = {}

        for plugin_id, state, contribution in results:
            status[plugin_id] = state
            if not isinstance(contribution, PluginContribution):
                continue

            plugin_payload[plugin_id] = contribution.to_dict()
            sections.extend(
                {**section.to_dict(), "plugin_id": plugin_id}
                for section in contribution.sections
            )
            retrieval_signals.extend(
                {
                    "plugin_id": plugin_id,
                    "key": signal.key,
                    "value": signal.value,
                    "role": signal.role,
                    "strength": signal.strength,
                    "entity_id": signal.entity_id,
                    "focus_text": signal.as_focus_text(),
                }
                for signal in contribution.retrieval_signals
                if signal.role != "none"
            )
            for section in contribution.sections:
                for field in section.fields:
                    if field.retrieval_role == "none":
                        continue
                    retrieval_signals.append(
                        {
                            "plugin_id": plugin_id,
                            "key": field.key,
                            "value": field.value,
                            "role": field.retrieval_role,
                            "strength": field.retrieval_strength,
                            "entity_id": None,
                            "focus_text": f"{field.key} {field.value}",
                        }
                    )
            actions.extend(
                {**action.to_dict(), "plugin_id": plugin_id}
                for action in contribution.actions
            )

        sections.sort(key=lambda item: (item.get("priority", 100), item.get("plugin_id", ""), item.get("key", "")))
        return {
            "plugins": plugin_payload,
            "sections": sections,
            "retrieval_signals": retrieval_signals,
            "actions": actions,
            "status": status,
        }
