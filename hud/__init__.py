"""HUD package public surface.

Keep package initialization lightweight. Importing a submodule such as
``aios_app.hud.context`` must not eagerly import the full HUD assembly stack,
because that stack depends on epistemic retrieval modules which in turn import
HUD context types.
"""

from __future__ import annotations

from typing import Any

__all__ = ["HUDAssembler", "render_hud_text"]


def __getattr__(name: str) -> Any:
    if name == "HUDAssembler":
        from aios_app.hud.frame import HUDAssembler

        return HUDAssembler
    if name == "render_hud_text":
        from aios_app.hud.render_text import render_hud_text

        return render_hud_text
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
