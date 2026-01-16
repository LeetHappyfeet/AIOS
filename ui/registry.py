# aios_app/ui/registry.py

from __future__ import annotations
from typing import Callable, List

# Each registered function is expected to add UI components
# (e.g. open a gr.Tab(...) context)
_TAB_RENDERERS: List[Callable[[], None]] = []


def register_tab(fn: Callable[[], None]) -> Callable[[], None]:
    """
    Register a UI tab renderer.

    The decorated function must create its UI when called.
    """
    _TAB_RENDERERS.append(fn)
    return fn


def render_all() -> None:
    """
    Render all registered tabs.

    Must be called inside a gr.Blocks() context.
    """
    for fn in _TAB_RENDERERS:
        fn()
