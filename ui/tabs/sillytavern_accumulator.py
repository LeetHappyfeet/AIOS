"""Operator-facing SillyTavern lorebook import into canonical cold corpora."""
from __future__ import annotations

import asyncio
from pathlib import Path
import re
import threading

import gradio as gr

from aios_app.config import settings
from aios_app.corpus_routing import ensure_knowledge_domain
from aios_app.db import Database
from aios_app.lorebook_import import import_lorebook, parse_lorebook
from aios_app.ui.registry import register_tab


_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True).start()
db = Database(settings.db_dsn)
def _sync(coro):
    return asyncio.run_coroutine_threadsafe(coro, _loop).result()
_sync(db.connect())


async def _domains():
    return await db.fetch(
        """SELECT domain_key, display_name FROM aios.knowledge_domain
           WHERE enabled ORDER BY lower(display_name), domain_key"""
    )


def _choices():
    return [f"{r['display_name']} — {r['domain_key']}" for r in _sync(_domains())]


def _key(selection):
    value = str(selection or "").strip()
    return value.rsplit(" — ", 1)[-1].strip() if value else ""


def _paths(files):
    if not files:
        return []
    return [Path(str(getattr(f, "name", f))) for f in (files if isinstance(files, (list, tuple)) else [files])]


def _preview(files, include_disabled):
    rows, errors = [], []
    for path in _paths(files):
        try:
            result = parse_lorebook(path, include_disabled=bool(include_disabled))
            rows.append([
                path.name, result["title"], result["format"],
                result["total_entries"], len(result["sections"]),
                result["disabled_entries"], result["sha256"][:12],
            ])
        except Exception as exc:
            errors.append(f"{path.name}: {exc}")
    return rows, "\n".join(errors) if errors else f"Validated {len(rows)} lorebook(s)."


async def _import(paths, selection, new_name, include_disabled):
    name = str(new_name or "").strip()
    if name:
        slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
        if not slug:
            raise ValueError("New universe name contains no usable letters/numbers")
        key = f"fiction.{slug}"
        # Never overwrite settings or re-enable an existing domain.
        existing = await db.fetchrow(
            "SELECT enabled FROM aios.knowledge_domain WHERE domain_key=$1", key
        )
        if existing and not existing["enabled"]:
            raise ValueError(f"Knowledge domain {key} exists but is disabled")
        if not existing:
            await ensure_knowledge_domain(
                db, domain_key=key, display_name=name,
                domain_kind="fictional_universe",
                default_scope_key=f"{key}.reference",
                default_epistemic_namespace=f"reference.{slug}",
                meta={"created_by": "gradio_lorebook_import"},
            )
    else:
        key = _key(selection)
    if not key:
        raise ValueError("Select a knowledge domain or enter a new universe name")
    if not paths:
        raise ValueError("Upload at least one .json lorebook")
    # Prevalidate all files before any DB changes.
    for path in paths:
        parse_lorebook(path, include_disabled=bool(include_disabled))
    results = []
    for path in paths:
        results.append(await import_lorebook(
            db, path, key, include_disabled=bool(include_disabled)
        ))
    return key, results


@register_tab
def render():
    with gr.Tab("Lorebook & World Knowledge"):
        gr.Markdown(
            """### Import SillyTavern lorebooks as AIOS reference knowledge

Upload standalone World Info JSON or portable character-book JSON. Each book
becomes a **cold corpus document**; its authored entries remain individual
searchable sections. Multiple lorebooks can share the same universe. Imports
are reference material only, not chat turns, experiences, or automatic beliefs.

Select an existing knowledge domain, or enter a new universe name. Source
keyword triggers, insertion position, and other SillyTavern runtime settings
are retained only as entry provenance.
"""
        )
        files = gr.File(label="Lorebook JSON files", file_count="multiple",
                        file_types=[".json"], type="filepath")
        include_disabled = gr.Checkbox(
            label="Include disabled lorebook entries as reference sections",
            value=False,
        )
        preview = gr.Button("Preview lorebooks")
        preview_table = gr.Dataframe(
            headers=["File", "Book title", "Format", "Entries",
                     "Importable sections", "Disabled", "SHA256"],
            interactive=False, label="Validation preview",
        )
        preview_status = gr.Textbox(label="Validation", interactive=False)
        with gr.Row():
            domain = gr.Dropdown(
                label="Existing universe / knowledge domain",
                choices=_choices(), value=None, scale=4,
            )
            refresh = gr.Button("Refresh universes", scale=1)
        new_name = gr.Textbox(
            label="Or create/use a fictional universe by display name (overrides selection)",
            placeholder="Original Fantasy World",
            info="Enter a plain-language name, not a domain key. Letters and numbers are required; spaces and punctuation are allowed. AIOS converts it to a lowercase fiction.<name-with-hyphens> key (for example: Original Fantasy World → fiction.original-fantasy-world). Leave blank to use the selected existing domain.",
        )
        submit = gr.Button("Import to corpus", variant="primary")
        results = gr.JSON(label="Imported corpus documents")
        status = gr.Textbox(label="Import status", interactive=False)

        preview.click(_preview, inputs=[files, include_disabled],
                      outputs=[preview_table, preview_status])
        refresh.click(lambda: gr.update(choices=_choices()), outputs=domain)

        def import_click(uploaded, selected, new_universe, include):
            try:
                key, imported = _sync(_import(
                    _paths(uploaded), selected, new_universe, include
                ))
            except Exception as exc:
                raise gr.Error(str(exc))
            choices = _choices()
            selected_label = next((c for c in choices if c.endswith(f" — {key}")), key)
            return (
                imported,
                f"Imported {len(imported)} lorebook(s) into {key}. "
                "Corpus indexing/research projections run asynchronously.",
                gr.update(choices=choices, value=selected_label),
            )

        submit.click(
            import_click,
            inputs=[files, domain, new_name, include_disabled],
            outputs=[results, status, domain],
        )
