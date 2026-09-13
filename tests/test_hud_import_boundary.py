from __future__ import annotations

import importlib
import sys


def test_hud_frame_import_does_not_require_world_runtime() -> None:
    """HUD cognition must not import the eager world runtime package on startup."""
    for name in (
        "aios_app.hud.frame",
        "aios_app.hud.retrieval",
        "aios_app.epistemic.halo_retrieval",
        "aios_app.epistemic.world_scope",
        "aios_app.world.runtime",
        "aios_app.world",
    ):
        sys.modules.pop(name, None)

    importlib.import_module("aios_app.epistemic.halo_retrieval")

    assert "aios_app.epistemic.world_scope" in sys.modules
    assert "aios_app.world.runtime" not in sys.modules
    assert "aios_app.world" not in sys.modules


def test_hud_frame_import_completes() -> None:
    module = importlib.import_module("aios_app.hud.frame")
    assert hasattr(module, "HUDAssembler")
