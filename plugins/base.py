from __future__ import annotations

from abc import ABC, abstractmethod

from aios_app.plugins.types import PluginContribution, PluginRuntimeContext


class AIOSPlugin(ABC):
    """Narrow runtime extension point for HUD/context providers."""

    plugin_id: str = "unknown"
    plugin_version: str = "0.0.0"
    enabled: bool = True
    timeout_seconds: float = 0.25

    async def startup(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    def applies_to(self, context: PluginRuntimeContext) -> bool:
        return True

    @abstractmethod
    async def collect(self, context: PluginRuntimeContext) -> PluginContribution:
        """Return ephemeral runtime context. This does not write DAG/RDF state."""
        raise NotImplementedError
