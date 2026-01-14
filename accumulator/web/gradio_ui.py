# accumulator/web/gradio_ui.py

import gradio as gr

from .queue import CrawlQueue, CrawlTask
from .worker import AccumulatorWorker

queue = CrawlQueue()
worker = AccumulatorWorker(queue)
worker.start()


def submit_url(url):
    queue.add(CrawlTask(url=url))
    return f"Queued ({queue.size()} pending): {url}"


with gr.Blocks() as demo:
    gr.Markdown("## Web Accumulator")

    url_input = gr.Textbox(label="URL")
    status = gr.Textbox(label="Status")

    submit = gr.Button("Add to crawl queue")
    submit.click(fn=submit_url, inputs=url_input, outputs=status)

if __name__ == "__main__":
    demo.launch(
        server_name="0.0.0.0",
        server_port=7860,
    )
