from __future__ import annotations

import asyncio
import json
import re
import threading
from typing import Any
from urllib.parse import urlparse

import gradio as gr

from aios_app.config import settings
from aios_app.corpus_catalog import CorpusCatalogService
from aios_app.corpus_routing import ensure_knowledge_domain, register_domain_identifier
from aios_app.db import Database
from aios_app.ui.registry import register_tab


_async_loop = asyncio.new_event_loop()
_thread = threading.Thread(target=_async_loop.run_forever, daemon=True)
_thread.start()


def run_async(coro):
    return asyncio.run_coroutine_threadsafe(coro, _async_loop).result()


db = Database(settings.db_dsn)
run_async(db.connect())


def _slug(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().casefold()).strip("-")
    return value


def _domain_key(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    return value if "." in value else f"fiction.{_slug(value)}"


def _domain_label(row: Any) -> str:
    return f"{row['display_name']} — {row['domain_key']}"


async def _domains() -> list[Any]:
    return await db.fetch(
        """SELECT kd.domain_key, kd.display_name, kd.domain_kind,
                  parent.domain_key AS parent_domain_key, kd.default_scope_key,
                  kd.default_epistemic_namespace, kd.enabled
           FROM aios.knowledge_domain kd
           LEFT JOIN aios.knowledge_domain parent ON parent.domain_id=kd.parent_domain_id
           ORDER BY lower(kd.display_name), kd.domain_key"""
    )


async def _domain_choices() -> list[str]:
    return [_domain_label(row) for row in await _domains() if row["enabled"]]


def _selected_key(selection: str) -> str:
    value = str(selection or "").strip()
    if " — " in value:
        return value.rsplit(" — ", 1)[1].strip()
    return value


async def _domain_table() -> list[list[Any]]:
    rows = await _domains()
    return [[
        r["display_name"], r["domain_key"], r["domain_kind"],
        r["parent_domain_key"] or "", r["default_scope_key"] or "",
        r["default_epistemic_namespace"], bool(r["enabled"]),
    ] for r in rows]


async def _domain_summary(domain_key: str) -> dict[str, Any]:
    row = await db.fetchrow(
        """SELECT kd.domain_key, kd.display_name, kd.domain_kind,
                  parent.domain_key AS parent_domain_key, kd.default_scope_key,
                  kd.default_epistemic_namespace, kd.enabled
           FROM aios.knowledge_domain kd
           LEFT JOIN aios.knowledge_domain parent ON parent.domain_id=kd.parent_domain_id
           WHERE kd.domain_key=$1""",
        domain_key,
    )
    if not row:
        return {"status": "domain not found"}
    counts = await db.fetchrow(
        """SELECT
             (SELECT count(*) FROM aios.corpus_document_domain WHERE knowledge_domain=$1) AS documents,
             (SELECT count(DISTINCT cs.section_id)
                FROM aios.corpus_document_domain cdd
                JOIN aios.corpus_section cs ON cs.document_id=cdd.document_id
               WHERE cdd.knowledge_domain=$1) AS sections,
             (SELECT count(*) FROM aios.character_knowledge_domain
               WHERE knowledge_domain=$1 AND enabled) AS characters,
             (SELECT count(*) FROM aios.corpus_source_profile
               WHERE knowledge_domain=$1 AND enabled) AS source_profiles,
             (SELECT count(*) FROM aios.knowledge_domain_identifier kdi
                JOIN aios.knowledge_domain kd ON kd.domain_id=kdi.domain_id
               WHERE kd.domain_key=$1) AS identifiers""",
        domain_key,
    )
    profiles = await db.fetch(
        """SELECT profile_key, domain_pattern, source_id, scope_key, epistemic_namespace
           FROM aios.corpus_source_profile
           WHERE knowledge_domain=$1 AND enabled
           ORDER BY priority DESC, profile_key""",
        domain_key,
    )
    characters = await db.fetch(
        """SELECT character_id, relationship, provenance
           FROM aios.character_knowledge_domain
           WHERE knowledge_domain=$1 AND enabled
           ORDER BY character_id""",
        domain_key,
    )
    return {
        "domain_key": row["domain_key"],
        "display_name": row["display_name"],
        "domain_kind": row["domain_kind"],
        "parent_domain": row["parent_domain_key"],
        "scope": row["default_scope_key"],
        "epistemic_namespace": row["default_epistemic_namespace"],
        "enabled": bool(row["enabled"]),
        "counts": dict(counts),
        "sources": [dict(p) for p in profiles],
        "characters": [dict(c) for c in characters],
    }


async def _identifiers(domain_key: str) -> list[list[Any]]:
    rows = await db.fetch(
        """SELECT kdi.identifier_type, kdi.identifier_value, kdi.source,
                  kdi.confidence
           FROM aios.knowledge_domain_identifier kdi
           JOIN aios.knowledge_domain kd ON kd.domain_id=kdi.domain_id
           WHERE kd.domain_key=$1
           ORDER BY kdi.identifier_type, kdi.identifier_value""",
        domain_key,
    )
    return [[r["identifier_type"], r["identifier_value"], r["source"], float(r["confidence"])] for r in rows]


async def _source_profiles(domain_key: str) -> list[list[Any]]:
    rows = await db.fetch(
        """SELECT csp.profile_key, csp.domain_pattern, csp.source_id,
                  csp.collection_key, csp.scope_key, cs.access_class,
                  csp.epistemic_namespace, csp.identity_binding
           FROM aios.corpus_source_profile csp
           JOIN aios.corpus_scope cs ON cs.scope_key=csp.scope_key
           WHERE csp.knowledge_domain=$1 AND csp.enabled
           ORDER BY csp.priority DESC, csp.profile_key""",
        domain_key,
    )
    return [[
        r["profile_key"], r["domain_pattern"] or "", r["source_id"] or "",
        r["collection_key"], r["scope_key"], r["access_class"],
        r["epistemic_namespace"], r["identity_binding"],
    ] for r in rows]


async def _create_domain(name: str, key: str, kind: str, parent: str, scope: str, namespace: str) -> dict:
    display = str(name or "").strip()
    if not display:
        raise ValueError("Display name is required")
    domain_key = _domain_key(key or display)
    if not domain_key:
        raise ValueError("Domain key is required")
    scope_key = str(scope or "").strip() or (f"{domain_key}.reference" if kind == "fictional_universe" else domain_key)
    ns = str(namespace or "").strip() or (
        f"canon.{domain_key.removeprefix('fiction.')}" if kind == "fictional_universe" else f"reference.{_slug(display)}"
    )
    return await ensure_knowledge_domain(
        db, domain_key=domain_key, display_name=display,
        domain_kind=str(kind or "general").strip(),
        parent_domain_key=str(parent or "").strip() or None,
        default_scope_key=scope_key,
        default_epistemic_namespace=ns,
        meta={"created_by": "gradio_knowledge_domains"},
    )


async def _add_identifier(domain_key: str, identifier_type: str, identifier_value: str) -> dict:
    if not domain_key:
        raise ValueError("Select a domain")
    return await register_domain_identifier(
        db, domain_key=domain_key,
        identifier_type=str(identifier_type or "").strip(),
        identifier_value=str(identifier_value or "").strip(),
        source="operator",
        meta={"created_by": "gradio_knowledge_domains"},
    )


async def _add_source_profile(domain_key: str, profile_key: str, url_or_host: str,
                              source_kind: str, access_class: str) -> dict:
    if not domain_key:
        raise ValueError("Select a domain")
    domain = await db.fetchrow(
        """SELECT display_name, default_scope_key, default_epistemic_namespace
           FROM aios.knowledge_domain WHERE domain_key=$1 AND enabled""",
        domain_key,
    )
    if not domain:
        raise ValueError("Unknown or disabled domain")
    raw = str(url_or_host or "").strip()
    if not raw:
        raise ValueError("Dedicated source host is required")
    host = (urlparse(raw if "://" in raw else f"https://{raw}").hostname or "").lower()
    if not host:
        raise ValueError("Could not determine source hostname")
    key = str(profile_key or "").strip() or f"{_slug(domain['display_name'])}-{_slug(host)}"
    collection = f"{_slug(domain['display_name'])}-{_slug(host)}"
    scope = domain["default_scope_key"] or f"{domain_key}.reference"
    result = await CorpusCatalogService(db).ensure_profile(
        profile_key=key, collection_key=collection, scope_key=scope,
        display_name=f"{domain['display_name']} — {host}",
        domain_pattern=host, knowledge_domain=domain_key,
        access_class=str(access_class or "domain"),
        epistemic_namespace=domain["default_epistemic_namespace"] or "reference",
        identity_binding="external", priority=100,
        meta={"created_by": "gradio_knowledge_domains", "source_kind": source_kind or "website"},
    )
    reclassified = await CorpusCatalogService(db).reclassify_profile(key)
    return {"profile": result, "reclassification": reclassified}


@register_tab
def render():
    with gr.Tab("Universes & Knowledge"):
        gr.Markdown(
            """### Universes & Knowledge
Knowledge domains connect fictional universes or real-world subject areas to corpus
sources and character affinity. Domains do not make a character know every fact and
are not runtime worlds; they provide the coherence layer that ties identity, corpus,
scope, and namespace together."""
        )
        with gr.Row():
            selector = gr.Dropdown(label="Universe / knowledge domain", choices=run_async(_domain_choices()), scale=4)
            refresh = gr.Button("Refresh", scale=1)
            load = gr.Button("Load", variant="primary", scale=1)
        summary = gr.JSON(label="Connected domain")

        with gr.Tabs():
            with gr.Tab("Create / Edit"):
                with gr.Row():
                    display_name = gr.Textbox(label="Display name", placeholder="Zoids")
                    domain_key = gr.Textbox(label="Domain key", placeholder="fiction.zoids")
                    domain_kind = gr.Dropdown(
                        choices=["fictional_universe", "professional_domain", "real_world_domain", "general"],
                        value="fictional_universe", label="Domain type",
                    )
                with gr.Row():
                    parent_key = gr.Textbox(label="Parent domain", placeholder="Optional")
                    scope_key = gr.Textbox(label="Default scope", placeholder="fiction.zoids.reference")
                    namespace = gr.Textbox(label="Epistemic namespace", placeholder="canon.zoids")
                create = gr.Button("Save universe / domain", variant="primary")
                create_status = gr.JSON(label="Result")

            with gr.Tab("Structured identifiers"):
                gr.Markdown("Trusted structured metadata such as fandom/franchise labels can route documents into this domain. Story prose is never used here.")
                with gr.Row():
                    identifier_type = gr.Dropdown(
                        choices=["fandom", "franchise", "universe", "subject", "domain"],
                        value="fandom", label="Identifier type",
                    )
                    identifier_value = gr.Textbox(label="Identifier value", placeholder="Zoids - All Media Types")
                    add_identifier = gr.Button("Register identifier")
                identifier_status = gr.JSON(label="Result")
                identifiers = gr.Dataframe(
                    headers=["type", "value", "source", "confidence"], interactive=False,
                    label="Registered identifiers",
                )

            with gr.Tab("Dedicated sources"):
                gr.Markdown("Use this for a host dedicated to the selected universe/domain. Do not use a shared host such as fandom.com or archiveofourown.org.")
                with gr.Row():
                    source_host = gr.Textbox(label="Dedicated host / URL", placeholder="zoids.fandom.com")
                    source_profile_key = gr.Textbox(label="Profile key", placeholder="Optional; generated automatically")
                with gr.Row():
                    source_kind = gr.Dropdown(
                        choices=["community_wiki", "official_wiki", "encyclopedia", "official_source", "website"],
                        value="community_wiki", label="Source kind",
                    )
                    access_class = gr.Dropdown(choices=["domain", "public", "restricted"], value="domain", label="Access class")
                    add_source = gr.Button("Add source profile")
                source_status = gr.JSON(label="Result")
                sources = gr.Dataframe(
                    headers=["profile", "host", "source_id", "collection", "scope", "access", "namespace", "binding"],
                    interactive=False, label="Connected source profiles",
                )

        def refresh_click():
            return gr.update(choices=run_async(_domain_choices()))

        def load_click(selection):
            key = _selected_key(selection)
            if not key:
                raise gr.Error("Select a universe / knowledge domain")
            return run_async(_domain_summary(key)), run_async(_identifiers(key)), run_async(_source_profiles(key))

        def create_click(name, key, kind, parent, scope, ns):
            try:
                result = run_async(_create_domain(name, key, kind, parent, scope, ns))
            except Exception as exc:
                raise gr.Error(str(exc))
            choices = run_async(_domain_choices())
            selection = next((v for v in choices if v.endswith(f" — {result['domain_key']}")), result["domain_key"])
            return result, gr.update(choices=choices, value=selection), run_async(_domain_summary(result["domain_key"]))

        def identifier_click(selection, kind, value):
            key = _selected_key(selection)
            try:
                result = run_async(_add_identifier(key, kind, value))
            except Exception as exc:
                raise gr.Error(str(exc))
            return result, run_async(_identifiers(key)), run_async(_domain_summary(key))

        def source_click(selection, profile, host, kind, access):
            key = _selected_key(selection)
            try:
                result = run_async(_add_source_profile(key, profile, host, kind, access))
            except Exception as exc:
                raise gr.Error(str(exc))
            return result, run_async(_source_profiles(key)), run_async(_domain_summary(key))

        refresh.click(fn=refresh_click, outputs=selector)
        load.click(fn=load_click, inputs=selector, outputs=[summary, identifiers, sources])
        create.click(
            fn=create_click,
            inputs=[display_name, domain_key, domain_kind, parent_key, scope_key, namespace],
            outputs=[create_status, selector, summary],
        )
        add_identifier.click(
            fn=identifier_click, inputs=[selector, identifier_type, identifier_value],
            outputs=[identifier_status, identifiers, summary],
        )
        add_source.click(
            fn=source_click, inputs=[selector, source_profile_key, source_host, source_kind, access_class],
            outputs=[source_status, sources, summary],
        )
