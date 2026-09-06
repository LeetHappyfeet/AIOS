from __future__ import annotations

from urllib.parse import urlparse
from uuid import UUID

import gradio as gr

from aios_app.ui.registry import register_tab
from aios_app.accumulator.web.config import OUTPUT_DIR
from aios_app.accumulator.web.queue import CrawlQueue, CrawlTask
from aios_app.accumulator.web.worker import AccumulatorWorker


queue = CrawlQueue(OUTPUT_DIR / ".crawl_state.json")
worker = AccumulatorWorker(queue)
worker.start()


def _clean_optional(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _derive_source_id(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if not host:
        raise ValueError("URL must include http:// or https:// and a hostname")
    return host


def _submit(
    url: str,
    source_id: str,
    source_kind: str,
    source_name: str,
    speaker_id: str,
    target_character_id: str,
    target_world_id: str,
    crawl_mode: str,
    max_depth: float,
    max_pages: float,
    same_domain_only: bool,
    respect_robots: bool,
):
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "URL must be a complete http:// or https:// URL.", _status_rows()

    source_id = (source_id or "").strip() or _derive_source_id(url)
    target_world = _clean_optional(target_world_id)
    if target_world:
        try:
            UUID(target_world)
        except ValueError:
            return "Target world must be a world UUID or left blank.", _status_rows()

    mode = "site" if crawl_mode == "Site crawl" else "page"
    task = CrawlTask(
        url=url,
        source_id=source_id,
        source_kind=(source_kind or "website").strip(),
        source_name=_clean_optional(source_name),
        speaker_id=_clean_optional(speaker_id),
        target_character_id=_clean_optional(target_character_id),
        target_world_id=target_world,
        crawl_mode=mode,
        max_depth=int(max_depth or 0) if mode == "site" else 0,
        max_pages=int(max_pages or 1) if mode == "site" else 1,
        same_domain_only=bool(same_domain_only),
        respect_robots=bool(respect_robots),
    )
    task_id = queue.add(task)
    return (
        f"Queued {task_id}. Source={source_id}; mode={mode}; "
        "content will enter liminal provenance before any character/world use.",
        _status_rows(),
    )


def _status_rows():
    return [
        [
            row["task_id"],
            row["state"],
            row["source_id"],
            row["pages_discovered"],
            row["pages_written"],
            row["pages_failed"],
            row["current_url"] or "",
            row["message"],
        ]
        for row in queue.recent(25)
    ]


@register_tab
def render():
    with gr.Tab("Web Accumulator"):
        gr.Markdown(
            """
### Web Accumulator

Fetch web material as **source observations**. A source or speaker is not a
character, and optional target character/world fields are routing hints only.
All scraped text enters the liminal DAG before downstream epistemic decisions.
"""
        )

        with gr.Row():
            with gr.Column(scale=2):
                url_input = gr.Textbox(
                    label="URL",
                    placeholder="https://example.com/article",
                )
                crawl_mode = gr.Radio(
                    ["Single page", "Site crawl"],
                    value="Single page",
                    label="Accumulation mode",
                )

                with gr.Row():
                    max_depth = gr.Number(
                        value=2,
                        precision=0,
                        minimum=0,
                        maximum=10,
                        label="Max crawl depth",
                    )
                    max_pages = gr.Number(
                        value=50,
                        precision=0,
                        minimum=1,
                        maximum=1000,
                        label="Max pages",
                    )

                with gr.Row():
                    same_domain_only = gr.Checkbox(
                        value=True,
                        label="Stay on seed domain",
                    )
                    respect_robots = gr.Checkbox(
                        value=True,
                        label="Respect robots.txt",
                    )

            with gr.Column(scale=2):
                source_id = gr.Textbox(
                    label="Source ID",
                    placeholder="Optional; defaults to hostname",
                    info="Durable source identity, e.g. cnn, memory_alpha, marvel_fandom.",
                )
                source_kind = gr.Dropdown(
                    choices=[
                        "website",
                        "news_organization",
                        "community_wiki",
                        "official_wiki",
                        "encyclopedia",
                        "official_source",
                        "forum",
                        "other_web",
                    ],
                    value="website",
                    allow_custom_value=True,
                    label="Source kind",
                )
                source_name = gr.Textbox(
                    label="Source display name",
                    placeholder="Optional human-readable source name",
                )
                speaker_id = gr.Textbox(
                    label="Speaker / publisher ID",
                    placeholder="Optional; leave blank when not reliably known",
                    info="Who asserts the page content when known. This is not character ownership.",
                )

        with gr.Accordion("Optional enrichment / world routing hints", open=False):
            gr.Markdown(
                "These fields do not assign the scrape to a character and do not assert it as world truth."
            )
            with gr.Row():
                target_character_id = gr.Textbox(
                    label="Target character ID",
                    placeholder="Optional enrichment target",
                )
                target_world_id = gr.Textbox(
                    label="Target world UUID",
                    placeholder="Optional world-routing target",
                )

        with gr.Row():
            submit = gr.Button("Start accumulation", variant="primary")
            refresh = gr.Button("Refresh status")

        status = gr.Textbox(label="Submission status", interactive=False)
        jobs = gr.Dataframe(
            headers=[
                "Task ID",
                "State",
                "Source",
                "Discovered",
                "Written",
                "Failed",
                "Current URL",
                "Message",
            ],
            datatype=["str", "str", "str", "number", "number", "number", "str", "str"],
            value=[],
            interactive=False,
            label="Recent accumulator jobs",
        )

        submit.click(
            fn=_submit,
            inputs=[
                url_input,
                source_id,
                source_kind,
                source_name,
                speaker_id,
                target_character_id,
                target_world_id,
                crawl_mode,
                max_depth,
                max_pages,
                same_domain_only,
                respect_robots,
            ],
            outputs=[status, jobs],
        )
        refresh.click(fn=_status_rows, inputs=None, outputs=jobs)
