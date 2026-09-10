from __future__ import annotations

import os

from aios_app.plugins.base import AIOSPlugin
from aios_app.plugins.types import (
    HUDField,
    HUDSection,
    PluginContribution,
    PluginRuntimeContext,
    RetrievalSignal,
)


class DemoStatusPlugin(AIOSPlugin):
    plugin_id = "demo_status"
    plugin_version = "0.1.0"
    enabled = os.getenv("AIOS_ENABLE_DEMO_HUD_PLUGIN", "").lower() in {"1", "true", "yes", "on"}
    timeout_seconds = 0.05

    async def collect(self, context: PluginRuntimeContext) -> PluginContribution:
        return PluginContribution(
            plugin_id=self.plugin_id,
            source="builtin-demo",
            state={"framework": "ready"},
            sections=(
                HUDSection(
                    key="plugin_demo",
                    title="PLUGIN STATUS",
                    priority=900,
                    fields=(
                        HUDField(key="framework", label="Framework", value="ready"),
                    ),
                ),
            ),
            retrieval_signals=(
                RetrievalSignal(key="plugin_framework", value="ready", role="context", strength=0.1),
            ),
        )


PLUGIN = DemoStatusPlugin()
