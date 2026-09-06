from __future__ import annotations

from urllib.parse import urlparse

import gradio as gr

from .queue import CrawlQueue, CrawlTask
from .worker import AccumulatorWorker


queue = CrawlQueue()
worker = AccumulatorWorker(queue)
worker.start()


def submit_url(url: str, source_id: str, crawl_site: bool):
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "A complete http:// or https:// URL is required."

    source_id = (source_id or "").strip() or parsed.netloc.lower()
    task = CrawlTask(
        url=url,
        source_id=source_id,
        crawl_mode="site" if crawl_site else "page",
        max_depth=2 if crawl_site else 0,
        max_pages=50 if crawl_site else 1,
    )
    task_id = queue.add(task)
    return f"Queued {task_id} for source {source_id}."


with gr.Blocks() as demo:
    gr.Markdown(
        "## Web Accumulator\n"
        "Standalone compatibility UI. The main AIOS UI exposes full provenance controls."
    )

    url_input = gr.Textbox(label="URL")
    source_id = gr.Textbox(label="Source ID", placeholder="Defaults to hostname")
    crawl_site = gr.Checkbox(label="Bounded same-domain crawl", value=False)
    status = gr.Textbox(label="Status")

    submit = gr.Button("Start accumulation")
    submit.click(
        fn=submit_url,
        inputs=[url_input, source_id, crawl_site],
        outputs=status,
    )

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=7860)
