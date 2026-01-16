from __future__ import annotations

import gradio as gr
from aios_app.ui.registry import register_tab

from aios_app.accumulator.web.queue import CrawlQueue, CrawlTask
from aios_app.accumulator.web.worker import AccumulatorWorker

# -------------------------------------------------
# Worker lifecycle (START ONCE)
# -------------------------------------------------

queue = CrawlQueue()
worker = AccumulatorWorker(queue)
worker.start()  # <-- start exactly once, at import time

# -------------------------------------------------
# UI Tab
# -------------------------------------------------

@register_tab
def render():
    with gr.Tab("Web Accumulator"):
        url_input = gr.Textbox(label="URL")
        status = gr.Textbox(label="Status")

        def submit(url: str):
            if not url:
                return "❌ URL required"
            queue.add(CrawlTask(url=url))
            return f"Queued ({queue.size()} pending): {url}"

        gr.Button("Add to crawl queue").click(
            fn=submit,
            inputs=url_input,
            outputs=status,
        )
