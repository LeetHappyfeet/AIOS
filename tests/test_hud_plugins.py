from __future__ import annotations

import asyncio
from uuid import uuid4

from aios_app.hud.render_text import render_hud_text
from aios_app.plugins.base import AIOSPlugin
from aios_app.plugins.manager import PluginManager
from aios_app.plugins.registry import PluginRegistry
from aios_app.plugins.types import (
    HUDField,
    HUDSection,
    PluginAction,
    PluginContribution,
    PluginRuntimeContext,
    RetrievalSignal,
)


def _context() -> PluginRuntimeContext:
    return PluginRuntimeContext(
        instance_id=uuid4(),
        character_id="test-character",
        entity_id=uuid4(),
        world_id=uuid4(),
        world_key="test-world",
        timeline_id=uuid4(),
        raw_state={},
    )


class TestPlugin(AIOSPlugin):
    plugin_id = "test_plugin"

    async def collect(self, context: PluginRuntimeContext) -> PluginContribution:
        return PluginContribution(
            plugin_id=self.plugin_id,
            state={"health": 34},
            sections=(
                HUDSection(
                    key="rpg",
                    title="RPG STATUS",
                    fields=(
                        HUDField(
                            key="health",
                            label="Health",
                            value=34,
                            field_type="gauge",
                            minimum=0,
                            maximum=100,
                        ),
                    ),
                ),
            ),
            retrieval_signals=(
                RetrievalSignal(key="condition", value="poisoned", role="pivot"),
            ),
            actions=(PluginAction(key="flee"),),
        )


class HangingPlugin(AIOSPlugin):
    plugin_id = "hanging_plugin"
    timeout_seconds = 0.01

    async def collect(self, context: PluginRuntimeContext) -> PluginContribution:
        await asyncio.sleep(1)
        return PluginContribution(plugin_id=self.plugin_id)


def test_plugin_manager_normalizes_contributions():
    registry = PluginRegistry()
    registry.register(TestPlugin())
    snapshot = asyncio.run(PluginManager(registry).collect(_context()))

    assert snapshot["status"]["test_plugin"] == "ok"
    assert snapshot["plugins"]["test_plugin"]["state"]["health"] == 34
    assert snapshot["sections"][0]["plugin_id"] == "test_plugin"
    assert snapshot["retrieval_signals"][0]["focus_text"] == "condition poisoned"
    assert snapshot["actions"][0]["key"] == "flee"


def test_plugin_timeout_isolated():
    registry = PluginRegistry()
    registry.register(HangingPlugin())
    snapshot = asyncio.run(PluginManager(registry).collect(_context()))

    assert snapshot["status"]["hanging_plugin"] == "timeout"
    assert snapshot["plugins"] == {}
    assert snapshot["sections"] == []


def test_text_renderer_includes_plugin_section():
    frame = {
        "identity": {"display_name": "Test"},
        "presence": {"world_key": "test", "instance_id": "i", "state_version": 1},
        "state": {},
        "scene": {},
        "plugin_sections": [
            {
                "plugin_id": "test_plugin",
                "key": "rpg",
                "title": "RPG STATUS",
                "fields": [
                    {
                        "key": "health",
                        "label": "Health",
                        "value": 34,
                        "field_type": "gauge",
                        "minimum": 0,
                        "maximum": 100,
                    }
                ],
            }
        ],
        "actions": ["speak", "flee"],
    }

    rendered = render_hud_text(frame)
    assert "RPG STATUS:" in rendered
    assert "Health: 34 / 100" in rendered
    assert "AVAILABLE ACTIONS: speak, flee" in rendered
