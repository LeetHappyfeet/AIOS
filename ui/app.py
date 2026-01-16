# aios_app/ui/app.py

from __future__ import annotations
import pkgutil
import aios_app.ui.tabs

import os
import logging
import gradio as gr

# -------------------------------------------------
# Auto-import all tab modules so they self-register
# -------------------------------------------------

for _, module_name, _ in pkgutil.iter_modules(
    aios_app.ui.tabs.__path__
):
    __import__(f"aios_app.ui.tabs.{module_name}")

# -------------------------------------------------
# IMPORTANT:
# Import tab modules so they self-register via @register_tab
# -------------------------------------------------

import aios_app.ui.tabs.web_accumulator

# Optional tabs – safe to comment out until ready
try:
    import aios_app.ui.tabs.character_editor
except ImportError:
    pass

from aios_app.ui.registry import render_all

logger = logging.getLogger("aios.ui")


# =================================================
# Build UI (registration happens at import time)
# =================================================

with gr.Blocks(title="AIOS Control Plane") as demo:
    render_all()


# =================================================
# Entrypoint
# =================================================

def run() -> None:
    host = os.getenv("AIOS_UI_HOST", "0.0.0.0")
    port = int(os.getenv("AIOS_UI_PORT", "7860"))

    logger.info("Starting AIOS UI on %s:%d", host, port)

    demo.launch(
        server_name=host,
        server_port=port,
        prevent_thread_lock=False,  # MUST block main thread
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
