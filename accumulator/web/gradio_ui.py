from __future__ import annotations

from urllib.parse import urlparse

import gradio as gr

from .config import OUTPUT_DIR
from .queue import CrawlQueue, CrawlTask
from .worker import AccumulatorWorker


queue = CrawlQueue(OUTPUT_DIR / ".crawl_state.json")
worker = AccumulatorWorker(queue)
worker.start()


def submit_url(url: str, source_id: str, corpus_profile_key: str, knowledge_domain: str, crawl_site: bool, allow_external_links: bool):
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "A complete http:// or https:// URL is required."

    source_id = (source_id or "").strip() or parsed.netloc.lower()
    corpus_profile_key = (corpus_profile_key or "").strip() or None
    knowledge_domain = (knowledge_domain or "").strip() or None
    task = CrawlTask(
        url=url,
        source_id=source_id,
        corpus_profile_key=corpus_profile_key,
        knowledge_domain=knowledge_domain,
        same_domain_only=not bool(allow_external_links),
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
    corpus_profile_key = gr.Textbox(label="Corpus profile (optional)", placeholder="Auto-routes by source/domain when blank")
    knowledge_domain = gr.Textbox(label="Declared knowledge domain (optional)", placeholder="Classifies the crawl seed only")
    crawl_site = gr.Checkbox(label="Crawl linked pages on this domain", value=False)
    allow_external_links = gr.Checkbox(label="Allow crawl to leave source domain (unsafe for corpus imports)", value=False)
    status = gr.Textbox(label="Status")

    submit = gr.Button("Start accumulation")
    submit.click(
        fn=submit_url,
        inputs=[url_input, source_id, corpus_profile_key, knowledge_domain, crawl_site, allow_external_links],
        outputs=status,
    )

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=7860)
