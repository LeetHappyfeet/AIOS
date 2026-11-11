"""Operator-controlled shadow diagnostics for structural corpus gaps."""
from __future__ import annotations

import asyncio
import threading

import gradio as gr

from aios_app.config import settings
from aios_app.db import Database
from aios_app.knowledge_enrichment.gaps import (
    list_structure_gaps, scan_structure_gaps, structure_gap_summary,
)
from aios_app.ui.registry import register_tab


_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True).start()
db = Database(settings.db_dsn)


def run_async(coro):
    return asyncio.run_coroutine_threadsafe(coro, _loop).result()


run_async(db.connect())


async def domain_choices():
    rows = await db.fetch(
        """SELECT domain_key, display_name FROM aios.knowledge_domain
           WHERE enabled ORDER BY lower(display_name),domain_key"""
    )
    return [f"{r['display_name']} — {r['domain_key']}" for r in rows]


def selected_domain(value):
    return str(value or "").rsplit(" — ", 1)[-1].strip()


@register_tab
def render():
    with gr.Tab("Corpus Structure Gaps"):
        gr.Markdown(
            """### Structural Gap Discovery (shadow mode)

Identify potential gaps between corpus vectors, Topic Atlas and the
source-qualified reference graph. Scans are **manual and bounded**:
they do not call an LLM, change RDF/Qdrant, modify memories, or affect HUDs.

**Semantic pair:** two topics co-occur at strong vector similarity in
at least two different source documents, but no verified Topic Atlas
relation connects them. **Dense/sparse:** an indexed, completely parsed
reference section contains six or more anchors and at most one extracted
relation. Neither condition establishes a factual relationship.
The detector examines a recent sample, not an exhaustive domain audit."""
        )
        with gr.Row():
            domain = gr.Dropdown(
                choices=run_async(domain_choices()),
                label="Knowledge domain", scale=4,
            )
            refresh = gr.Button("Refresh domains", scale=1)
        limit = gr.Slider(
            minimum=25, maximum=500, step=25, value=200,
            label="Maximum corpus sections to sample per scan",
        )
        with gr.Row():
            scan = gr.Button("Scan for structural gaps", variant="primary")
            reload = gr.Button("Load existing candidates")
        scan_report = gr.JSON(label="Last scan receipt")
        summary = gr.JSON(label="Current candidate counts")
        candidates = gr.Dataframe(
            headers=["Gap ID", "Kind", "Status", "Subject", "Related", "Sections", "Documents", "Revision"],
            interactive=False, label="Candidate evidence (latest 100)",
        )
        detail = gr.JSON(label="Candidate signals (latest 100)")

        def load_click(selected):
            key = selected_domain(selected)
            if not key:
                raise gr.Error("Select a knowledge domain")
            try:
                counts = run_async(structure_gap_summary(db,domain_key=key))
                rows = run_async(list_structure_gaps(db,domain_key=key))
            except Exception as exc:
                raise gr.Error(str(exc))
            return counts, [
                [r["gap_id"],r["kind"],r["status"],r["subject"],r["related"],
                 r["section_count"],r["document_count"],r["revision"]]
                for r in rows
            ], rows

        def scan_click(selected, count):
            key = selected_domain(selected)
            if not key:
                raise gr.Error("Select a knowledge domain")
            try:
                receipt = run_async(scan_structure_gaps(db,domain_key=key,limit=int(count)))
                counts, rows, details = load_click(selected)
            except Exception as exc:
                raise gr.Error(str(exc))
            return receipt, counts, rows, details

        refresh.click(lambda: gr.update(choices=run_async(domain_choices())),outputs=domain)
        reload.click(load_click,inputs=domain,outputs=[summary,candidates,detail])
        scan.click(scan_click,inputs=[domain,limit],outputs=[scan_report,summary,candidates,detail])
