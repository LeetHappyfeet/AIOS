from __future__ import annotations

from urllib.parse import urlparse
from uuid import UUID

import gradio as gr

from aios_app.ui.registry import register_tab
from aios_app.accumulator.web.config import OUTPUT_DIR
from aios_app.accumulator.web.queue import CrawlQueue, CrawlTask
from aios_app.accumulator.web.worker import AccumulatorWorker
from aios_app.config import settings
from aios_app.db import Database

import asyncio
import threading


queue = CrawlQueue(OUTPUT_DIR / ".crawl_state.json")
worker = AccumulatorWorker(queue)
worker.start()

_domain_loop = asyncio.new_event_loop()
_domain_thread = threading.Thread(target=_domain_loop.run_forever, daemon=True)
_domain_thread.start()
_domain_db = Database(settings.db_dsn)
asyncio.run_coroutine_threadsafe(_domain_db.connect(), _domain_loop).result()


def _run_domain(coro):
    return asyncio.run_coroutine_threadsafe(coro, _domain_loop).result()


async def _domain_choices():
    rows = await _domain_db.fetch(
        """SELECT display_name, domain_key FROM aios.knowledge_domain
           WHERE enabled ORDER BY lower(display_name), domain_key"""
    )
    return [f"{r['display_name']} — {r['domain_key']}" for r in rows]


def _domain_key(selection: str | None) -> str | None:
    value = (selection or "").strip()
    if not value:
        return None
    return value.rsplit(" — ", 1)[1].strip() if " — " in value else value


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
    ingest_mode: str,
    consumption_mode: str,
    corpus_profile_key: str,
    knowledge_domain: str,
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
    ingest_value = {"Store in corpus": "corpus", "Store and read as target": "consume", "Legacy semantic ingestion": "semantic"}.get(ingest_mode, "corpus")
    target_character = _clean_optional(target_character_id)
    if ingest_value == "consume" and not target_character:
        return "Store and read requires a target character ID.", _status_rows()
    task = CrawlTask(
        url=url,
        source_id=source_id,
        source_kind=(source_kind or "website").strip(),
        source_name=_clean_optional(source_name),
        speaker_id=_clean_optional(speaker_id),
        target_character_id=target_character,
        target_world_id=target_world,
        ingest_mode=ingest_value,
        consumption_mode=(consumption_mode or "read").strip().lower(),
        corpus_profile_key=_clean_optional(corpus_profile_key),
        knowledge_domain=_domain_key(knowledge_domain),
        crawl_mode=mode,
        max_depth=int(max_depth or 0) if mode == "site" else 0,
        max_pages=int(max_pages or 1) if mode == "site" else 1,
        same_domain_only=bool(same_domain_only),
        respect_robots=bool(respect_robots),
    )
    task_id = queue.add(task)
    return (
        f"Queued {task_id}. Source={source_id}; crawl={mode}; ingestion={ingest_value}. "
        + ("Selected content will be explicitly consumed by the target character." if ingest_value == "consume" else "Accumulated content does not imply character knowledge."),
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

Fetch web material into the **cold searchable corpus** by default. Storing a page does not make any character know it and does not assert it as world truth. Use **Store and read as target** only when the selected character should explicitly acquire the page.
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

        with gr.Row():
            ingest_mode = gr.Radio(
                ["Store in corpus", "Store and read as target", "Legacy semantic ingestion"],
                value="Store in corpus",
                label="Ingestion behavior",
                info="Corpus storage is cold/searchable only. Read creates explicit character acquisition.",
            )
            consumption_mode = gr.Dropdown(
                choices=["read", "research", "taught", "import"],
                value="read",
                label="Acquisition mode",
                info="Used only with Store and read as target.",
            )

        with gr.Accordion("Corpus classification", open=False):
            gr.Markdown(
                "Use a source profile for a dedicated repository/host. A declared knowledge domain classifies only the crawl seed; linked descendants must qualify independently."
            )
            with gr.Row():
                corpus_profile_key = gr.Textbox(
                    label="Corpus profile key",
                    placeholder="Optional trusted source profile",
                )
                knowledge_domain = gr.Dropdown(
                    label="Universe / declared knowledge domain",
                    choices=_run_domain(_domain_choices()),
                    allow_custom_value=True,
                    info="Select a registered domain. For site crawls this declaration classifies only the seed; descendants must qualify independently.",
                )
                refresh_domains = gr.Button("Refresh registered domains")

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
                ingest_mode,
                consumption_mode,
                corpus_profile_key,
                knowledge_domain,
                crawl_mode,
                max_depth,
                max_pages,
                same_domain_only,
                respect_robots,
            ],
            outputs=[status, jobs],
        )
        refresh.click(fn=_status_rows, inputs=None, outputs=jobs)

        def refresh_domain_choices():
            return gr.update(choices=_run_domain(_domain_choices()))

        refresh_domains.click(fn=refresh_domain_choices, outputs=knowledge_domain)
