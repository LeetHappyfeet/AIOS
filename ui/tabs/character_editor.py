# aios_app/ui/tabs/character_editor.py
from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

import gradio as gr

from aios_app.config import settings
from aios_app.char.identity_kernel import IdentityKernelStore
from aios_app.char.identity_revision import accept_identity_candidate, reject_identity_candidate
from aios_app.char.identity_sources import identity_snapshot, stage_identity_source
from aios_app.corpus_catalog import CorpusAccessReconciler
from aios_app.db import Database
from aios_app.epistemic.weights import (
    DEFAULT_PROFILE as DEFAULT_EPISTEMIC_PROFILE,
    get_profile as get_epistemic_profile,
    upsert_profile as upsert_epistemic_profile,
)
from aios_app.hud.profile import (
    bind_character_profile,
    get_profile as get_hud_profile,
    get_profile_by_name,
    list_profiles,
    save_profile,
)
from aios_app.ui.registry import register_tab


_async_loop = asyncio.new_event_loop()
_thread = threading.Thread(target=_async_loop.run_forever, daemon=True)
_thread.start()


def run_async(coro):
    return asyncio.run_coroutine_threadsafe(coro, _async_loop).result()


db = Database(settings.db_dsn)
run_async(db.connect())


def _json_object(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(decoded) if isinstance(decoded, dict) else {}
    try:
        decoded = json.loads(str(value))
        return dict(decoded) if isinstance(decoded, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _mapping_rows(value: Any) -> list[list[Any]]:
    mapping = _json_object(value)
    return [[str(key), val] for key, val in sorted(mapping.items())]


def _rows_mapping(rows: Any) -> dict[str, float]:
    result: dict[str, float] = {}
    for row in rows or []:
        if not row or len(row) < 2:
            continue
        key = str(row[0] or "").strip()
        if not key:
            continue
        result[key] = float(row[1])
    return result


async def _character_choices() -> list[str]:
    rows = await db.fetch(
        """
        SELECT character_id, COALESCE(display_name, canonical_name, character_id) AS label
        FROM aios.character_identity
        ORDER BY lower(COALESCE(display_name, canonical_name, character_id)), character_id
        """
    )
    return [
        f"{row['label']}  [{row['character_id']}]"
        if row["label"] != row["character_id"] else str(row["character_id"])
        for row in rows
    ]


def _character_id(selection: str) -> str:
    value = str(selection or "").strip()
    if value.endswith("]") and "  [" in value:
        return value.rsplit("  [", 1)[1][:-1]
    return value


async def _load_character(character_id: str):
    return await db.fetchrow(
        "SELECT * FROM aios.character_identity WHERE character_id=$1",
        character_id,
    )


async def _save_character(character_id: str, values: dict[str, Any]) -> None:
    if not await db.fetchrow(
        "SELECT 1 FROM aios.character_identity WHERE character_id=$1", character_id
    ):
        raise ValueError(f"Unknown character_id '{character_id}'")
    await db.execute(
        """
        UPDATE aios.character_identity
        SET canonical_name=$2, display_name=$3, canon=$4, franchise=$5,
            entity_type=$6, species=$7, gender=$8, age_descriptor=$9,
            visual_summary=$10, primary_role=$11, archetype=$12,
            default_tone=$13, speech_style=$14, content_rating=$15,
            moral_constraints=$16, process_ontology=$17,
            is_canonical=$18, is_mutable=$19, updated_at=now()
        WHERE character_id=$1
        """,
        character_id,
        values["canonical_name"] or None,
        values["display_name"] or None,
        values["canon"] or None,
        values["franchise"] or None,
        values["entity_type"] or "character",
        values["species"] or None,
        values["gender"] or None,
        values["age_descriptor"] or None,
        values["visual_summary"] or None,
        values["primary_role"] or None,
        values["archetype"] or None,
        values["default_tone"] or None,
        values["speech_style"] or None,
        values["content_rating"] or "PG",
        values["moral_constraints"] or None,
        bool(values["process_ontology"]),
        bool(values["is_canonical"]),
        bool(values["is_mutable"]),
    )


async def _identity_view(character_id: str):
    snapshot = await identity_snapshot(db, character_id)
    kernel = await IdentityKernelStore(db).get(character_id)
    identity = snapshot["identity"]
    overview = {
        "character_id": character_id,
        "display_name": identity.get("display_name") or identity.get("canonical_name") or character_id,
        "identity_version": int(identity.get("identity_version") or 1),
        "compiler_version": kernel.compiler_version if kernel else None,
        "active_facets": sum(1 for row in snapshot["facets"] if row.get("status") == "active"),
        "pending_candidates": sum(1 for row in snapshot["candidates"] if row.get("disposition") == "proposed"),
        "identity_sources": len(snapshot["sources"]),
        "revisions": len(snapshot["revisions"]),
    }
    facets = [[
        str(r["facet_id"]), r["facet_type"], r["facet_key"],
        json.dumps(r["value"], ensure_ascii=False), r["stability"], r["authority"],
        r["mutability"], r["status"], str(r.get("source_id") or ""),
    ] for r in snapshot["facets"]]
    sources = [[
        str(r["source_id"]), r["source_type"], r.get("source_format") or "",
        r.get("source_name") or "", r["authority"], str(r["imported_at"]),
    ] for r in snapshot["sources"]]
    candidates = [[
        str(r["candidate_id"]), r["facet_type"], r["facet_key"],
        json.dumps(r["value"], ensure_ascii=False), r["stability"], r["authority"],
        r["mutability"], r["perspective"], r.get("continuity_key") or "",
        r["disposition"], str(r.get("source_id") or ""),
    ] for r in snapshot["candidates"]]
    revisions = [[
        r["identity_version"], r["operation"], str(r.get("facet_id") or ""),
        str(r.get("candidate_id") or ""), r["actor"], r.get("reason") or "",
        str(r["created_at"]),
    ] for r in reversed(snapshot["revisions"])]
    return overview, (kernel.kernel_text if kernel else ""), facets, sources, candidates, revisions


async def _stage_manual_facet(character_id: str, facet_type: str, facet_key: str,
                              value_text: str, stability: str, mutability: str,
                              perspective: str, authority: str) -> dict:
    facet_type = str(facet_type or "").strip()
    facet_key = str(facet_key or "").strip()
    if not facet_type or not facet_key:
        raise ValueError("Facet type and key are required")
    try:
        value = json.loads(value_text)
    except (TypeError, json.JSONDecodeError):
        value = value_text
    payload = {"facet_type": facet_type, "facet_key": facet_key, "value": value}
    return await stage_identity_source(
        db, character_id=character_id, source_type="manual",
        source_name="Character Control", source_format="gradio",
        authority=str(authority or "authored"),
        payload=payload,
        candidates=[{
            "facet_type": facet_type, "facet_key": facet_key, "value": value,
            "stability": stability or "core", "mutability": mutability or "explicit",
            "perspective": perspective or "self", "authority": authority or "authored",
            "source_field": facet_key,
        }],
        meta={"ui": "character_control"},
    )


async def _registered_domain_choices() -> list[str]:
    rows = await db.fetch(
        """SELECT display_name, domain_key FROM aios.knowledge_domain
           WHERE enabled ORDER BY lower(display_name), domain_key"""
    )
    return [f"{row['display_name']} — {row['domain_key']}" for row in rows]


def _registered_domain_key(selection: str) -> str:
    value = str(selection or "").strip()
    return value.rsplit(" — ", 1)[1].strip() if " — " in value else value


async def _add_character_domain(character_id: str, selection: str) -> dict:
    domain = _registered_domain_key(selection)
    if not domain:
        raise ValueError("Select a registered universe / knowledge domain")
    current = await db.fetch(
        """SELECT knowledge_domain FROM aios.character_knowledge_domain
           WHERE character_id=$1 AND enabled""",
        character_id,
    )
    domains = sorted({str(row["knowledge_domain"]) for row in current} | {domain})
    return await CorpusAccessReconciler(db).set_character_domains(character_id, domains)


async def _domain_rows(character_id: str) -> list[list[Any]]:
    rows = await db.fetch(
        """
        SELECT knowledge_domain, enabled
        FROM aios.character_knowledge_domain
        WHERE character_id=$1
        ORDER BY knowledge_domain
        """,
        character_id,
    )
    return [[row["knowledge_domain"], bool(row["enabled"])] for row in rows]


async def _save_domains(character_id: str, rows: Any) -> dict:
    domains = [
        str(row[0]).strip()
        for row in (rows or [])
        if row and len(row) >= 1 and str(row[0] or "").strip()
        and (len(row) < 2 or bool(row[1]))
    ]
    return await CorpusAccessReconciler(db).set_character_domains(character_id, domains)


async def _access_rows(character_id: str) -> list[list[Any]]:
    rows = await db.fetch(
        """
        SELECT scope_key, allowed, COALESCE(meta, '{}'::jsonb) AS meta
        FROM aios.character_corpus_access
        WHERE character_id=$1
        ORDER BY scope_key, allowed DESC
        """,
        character_id,
    )
    return [
        [row["scope_key"], "allow" if row["allowed"] else "deny", json.dumps(_json_object(row["meta"]))]
        for row in rows
    ]


async def _runtime_rows(character_id: str) -> list[list[Any]]:
    rows = await db.fetch(
        """
        SELECT ci.instance_id, w.world_key, rs.world_id, rs.timeline_id,
               rs.head_node_id, rs.source_timeline_id, rs.source_head_node_id,
               rs.lifecycle_state, rs.state_version,
               COALESCE(ec.controller_type, '') AS controller_type,
               COALESCE(ec.controller_ref, '') AS controller_ref
        FROM aios.character_instance ci
        LEFT JOIN aios.character_runtime_state rs ON rs.instance_id=ci.instance_id
        LEFT JOIN aios.world w ON w.world_id=rs.world_id
        LEFT JOIN LATERAL (
            SELECT controller_type, controller_ref
            FROM aios.world_entity we
            JOIN aios.entity_controller ec ON ec.entity_id=we.entity_id
            WHERE we.character_instance_id=ci.instance_id AND ec.active=true
            ORDER BY CASE ec.authority WHEN 'primary' THEN 0 ELSE 1 END, ec.created_at
            LIMIT 1
        ) ec ON true
        WHERE ci.character_id=$1
        ORDER BY rs.updated_at DESC NULLS LAST, ci.created_at DESC
        """,
        character_id,
    )
    return [[
        str(r["instance_id"]), r["world_key"] or "", str(r["world_id"] or ""),
        str(r["timeline_id"] or ""), str(r["head_node_id"] or ""),
        str(r["source_timeline_id"] or ""), str(r["source_head_node_id"] or ""),
        r["lifecycle_state"] or "", r["state_version"] if r["state_version"] is not None else "",
        r["controller_type"], r["controller_ref"],
    ] for r in rows]


async def _scene_state(character_id: str) -> dict:
    row = await db.fetchrow(
        """
        SELECT css.snapshot_id, css.instance_id, css.runtime_timeline_id,
               css.runtime_head_node_id, css.source_timeline_id,
               css.source_head_node_id, css.scene_state, css.evidence_node_ids,
               css.projection_version, css.updated_at
        FROM aios.character_scene_snapshot css
        JOIN aios.character_instance ci ON ci.instance_id=css.instance_id
        WHERE ci.character_id=$1
        ORDER BY css.updated_at DESC
        LIMIT 1
        """,
        character_id,
    )
    if not row:
        return {"status": "no scene snapshot"}
    return {
        "snapshot_id": str(row["snapshot_id"]),
        "instance_id": str(row["instance_id"]),
        "runtime_timeline_id": str(row["runtime_timeline_id"]),
        "runtime_head_node_id": str(row["runtime_head_node_id"] or ""),
        "source_timeline_id": str(row["source_timeline_id"] or ""),
        "source_head_node_id": str(row["source_head_node_id"] or ""),
        "projection_version": row["projection_version"],
        "updated_at": str(row["updated_at"]),
        "evidence_node_ids": [str(v) for v in (row["evidence_node_ids"] or [])],
        "scene_state": _json_object(row["scene_state"]),
    }


async def _research_rows(character_id: str, limit: int = 30) -> list[list[Any]]:
    rows = await db.fetch(
        """
        SELECT cre.research_id, cre.instance_id, cre.query_text, cre.status,
               cre.result_count, cre.created_at,
               count(cce.section_id) AS exposures,
               count(*) FILTER (WHERE cce.acquisition_status='acquired') AS acquired
        FROM aios.character_research_event cre
        LEFT JOIN aios.character_corpus_exposure cce ON cce.research_id=cre.research_id
        WHERE cre.character_id=$1
        GROUP BY cre.research_id, cre.instance_id, cre.query_text, cre.status,
                 cre.result_count, cre.created_at
        ORDER BY cre.created_at DESC
        LIMIT $2
        """,
        character_id, max(1, min(int(limit), 200)),
    )
    return [[
        str(r["research_id"]), str(r["instance_id"]), str(r["created_at"]),
        r["query_text"], r["status"], r["result_count"], r["exposures"], r["acquired"],
    ] for r in rows]


async def _speaker_rows(character_id: str, limit: int = 30) -> list[list[Any]]:
    rows = await db.fetch(
        """
        SELECT ie.event_id, ie.event_time, ie.speaker_id,
               ie.speaker_role::text AS speaker_type, ie.character_id,
               ie.viewpoint_id, ie.session_id, dn.node_id, dn.timeline_id,
               left(COALESCE(ie.message_text,''), 180) AS message_text
        FROM aios.ingest_event ie
        LEFT JOIN aios.dag_node dn ON dn.event_id=ie.event_id
        WHERE ie.character_id=$1
        ORDER BY ie.event_id DESC
        LIMIT $2
        """,
        character_id, max(1, min(int(limit), 200)),
    )
    return [[
        r["event_id"], str(r["event_time"] or ""), r["speaker_id"] or "",
        r["speaker_type"] or "", r["character_id"] or "", r["viewpoint_id"] or "",
        str(r["session_id"] or ""), str(r["node_id"] or ""),
        str(r["timeline_id"] or ""), r["message_text"] or "",
    ] for r in rows]


def _hud_values(profile) -> list[Any]:
    return [
        profile.profile_name, profile.description or "", profile.token_budget,
        profile.recent_event_limit, profile.memory_budget, profile.belief_budget,
        profile.relationship_budget, profile.scene_budget, profile.inventory_budget,
        profile.rules_budget, profile.goals_budget, profile.entity_hops,
        profile.semantic_retrieval_limit, profile.deep_memory_limit,
        profile.include_emotional_state, profile.include_physical_state,
        profile.include_social_state, profile.include_inventory,
        profile.include_relationships, profile.include_conflicts,
        profile.include_provenance, profile.include_confidence,
    ]


def _epistemic_values(profile: dict) -> list[Any]:
    p = {**DEFAULT_EPISTEMIC_PROFILE, **dict(profile or {})}
    return [
        float(p["skepticism"]), float(p["curiosity"]), float(p["authority_trust"]),
        float(p["novelty_seeking"]), float(p["emotional_reactivity"]),
        float(p["retention"]), str(p.get("memory_continuity") or "character"),
        _mapping_rows(p.get("source_trust")), _mapping_rows(p.get("topic_interest")),
        _mapping_rows(p.get("domain_expertise")), _mapping_rows(p.get("trait_weights")),
    ]


@register_tab
def render():
    with gr.Tab("Character Control"):
        gr.Markdown(
            """
            ### Character Control
            Durable identity, epistemic/learning behavior, corpus affinity, HUD
            presentation, and read-only runtime diagnostics. Corpus affinity changes
            relevance; it does **not** grant access and does **not** create knowledge.
            """
        )

        with gr.Row():
            character_selector = gr.Dropdown(
                label="Character", choices=run_async(_character_choices()),
                allow_custom_value=True, scale=4,
            )
            refresh_characters = gr.Button("Refresh list", scale=1)
            load_character = gr.Button("Load", variant="primary", scale=1)
            refresh_diagnostics = gr.Button("Refresh diagnostics", scale=1)
        status = gr.Markdown()

        with gr.Tabs():
            with gr.Tab("Overview"):
                character_overview = gr.JSON(label="Character summary")
                gr.Markdown("The summary reflects durable identity plus the current identity-kernel lifecycle.")

            with gr.Tab("Identity"):
                gr.Markdown(
                    "Identity is versioned and provenance-backed. New identity material is staged as a candidate; "
                    "only acceptance changes durable facets and recompiles the runtime kernel."
                )
                with gr.Tabs():
                    with gr.Tab("Kernel"):
                        identity_kernel_text = gr.Textbox(
                            label="Compiled runtime identity kernel", lines=16, interactive=False
                        )
                    with gr.Tab("Facets"):
                        identity_facets = gr.Dataframe(
                            headers=["facet_id","type","key","value","stability","authority",
                                     "mutability","status","source_id"],
                            interactive=False, label="Durable identity facets",
                        )
                        with gr.Accordion("Propose manual identity facet", open=False):
                            with gr.Row():
                                new_facet_type = gr.Dropdown(
                                    choices=["identity","appearance","personality","values","expression",
                                             "constraint","role","developmental","domain"],
                                    value="personality", allow_custom_value=True, label="Facet type",
                                )
                                new_facet_key = gr.Textbox(label="Facet key")
                            new_facet_value = gr.Textbox(
                                label="Value (plain text or JSON)", lines=3
                            )
                            with gr.Row():
                                new_facet_stability = gr.Dropdown(
                                    choices=["structural","constitutional","core","developmental"],
                                    value="core", label="Stability",
                                )
                                new_facet_mutability = gr.Dropdown(
                                    choices=["locked","explicit","developmental"],
                                    value="explicit", label="Mutability",
                                )
                                new_facet_perspective = gr.Dropdown(
                                    choices=["self","biographical","public_reputation","secret","unknown"],
                                    value="self", label="Perspective",
                                )
                                new_facet_authority = gr.Textbox(label="Authority", value="authored")
                            stage_facet = gr.Button("Stage candidate", variant="primary")
                    with gr.Tab("Sources & Candidates"):
                        identity_sources = gr.Dataframe(
                            headers=["source_id","type","format","name","authority","imported_at"],
                            interactive=False, label="Identity sources",
                        )
                        identity_candidates = gr.Dataframe(
                            headers=["candidate_id","type","key","value","stability","authority",
                                     "mutability","perspective","continuity","disposition","source_id"],
                            interactive=False, label="Identity candidates",
                        )
                        with gr.Row():
                            candidate_id = gr.Textbox(label="Candidate ID")
                            candidate_reason = gr.Textbox(label="Decision reason")
                        with gr.Row():
                            accept_candidate = gr.Button("Accept candidate", variant="primary")
                            reject_candidate = gr.Button("Reject candidate")
                    with gr.Tab("Revisions"):
                        identity_revisions = gr.Dataframe(
                            headers=["version","operation","facet_id","candidate_id","actor","reason","created_at"],
                            interactive=False, label="Identity revision history",
                        )
                    with gr.Tab("Compatibility Record"):
                        gr.Markdown(
                            "Legacy root fields remain visible for migration/compatibility. "
                            "They are no longer the primary identity editing surface."
                        )
                        legacy_identity = gr.JSON(label="character_identity root record")

            with gr.Tab("Epistemics & Learning"):
                gr.Markdown(
                    "These values are consumed by AIOS attention, trust, retention, belief weighting, and corpus-learning policy."
                )
                with gr.Row():
                    skepticism = gr.Slider(0, 1, value=.5, step=.01, label="Skepticism")
                    curiosity = gr.Slider(0, 1, value=.5, step=.01, label="Curiosity")
                    authority_trust = gr.Slider(0, 1, value=.5, step=.01, label="Authority trust")
                with gr.Row():
                    novelty_seeking = gr.Slider(0, 1, value=.5, step=.01, label="Novelty seeking")
                    emotional_reactivity = gr.Slider(0, 1, value=.5, step=.01, label="Emotional reactivity")
                    retention = gr.Slider(0, 1, value=.7, step=.01, label="Retention")
                memory_continuity = gr.Dropdown(
                    label="Memory continuity", choices=["character","user","isolated"], value="character",
                    info="character: across character_id; user: same runtime user; isolated: current experiential lineage",
                )
                source_trust = gr.Dataframe(
                    headers=["source / source_key", "trust"], datatype=["str","number"],
                    row_count=(2, "dynamic"), col_count=(2, "fixed"), type="array", label="Source trust",
                )
                topic_interest = gr.Dataframe(
                    headers=["topic / scope", "interest"], datatype=["str","number"],
                    row_count=(2, "dynamic"), col_count=(2, "fixed"), type="array", label="Topic interest",
                )
                domain_expertise = gr.Dataframe(
                    headers=["domain / concept", "expertise"], datatype=["str","number"],
                    row_count=(2, "dynamic"), col_count=(2, "fixed"), type="array", label="Domain expertise",
                )
                with gr.Accordion("Advanced trait weights", open=False):
                    trait_weights = gr.Dataframe(
                        headers=["trait", "weight"], datatype=["str","number"],
                        row_count=(1, "dynamic"), col_count=(2, "fixed"), type="array", label="Trait weights",
                    )
                save_epistemics = gr.Button("Save epistemic profile")

            with gr.Tab("Knowledge & Research"):
                gr.Markdown(
                    """
                    **Research domains are affinity, not knowledge and not authorization.**
                    They boost corpus routing/relevance. Shared corpus remains searchable
                    without a domain assignment; restricted corpus uses explicit ACLs.
                    """
                )
                with gr.Row():
                    registered_domain = gr.Dropdown(
                        label="Add registered universe / domain",
                        choices=run_async(_registered_domain_choices()),
                        allow_custom_value=True,
                        scale=4,
                    )
                    add_registered_domain = gr.Button("Add affinity", scale=1)
                    refresh_registered_domains = gr.Button("Refresh domains", scale=1)
                knowledge_domains = gr.Dataframe(
                    headers=["knowledge_domain", "enabled"], datatype=["str","bool"],
                    row_count=(2, "dynamic"), col_count=(2, "fixed"),
                    label="Research / knowledge-domain affinity",
                )
                save_domains = gr.Button("Save domain affinity")
                affinity_status = gr.JSON(label="Resolved affinity")
                explicit_access = gr.Dataframe(
                    headers=["scope", "rule", "metadata"], interactive=False,
                    label="Explicit corpus ACLs (read-only security diagnostics)",
                )
                gr.Markdown("Recent corpus lookup and acquisition activity:")
                research_limit = gr.Number(label="Recent research events", value=30, precision=0)
                research_table = gr.Dataframe(
                    headers=["research_id","instance_id","time","query","status","results","exposures","acquired"],
                    interactive=False, label="Corpus research history",
                )

            with gr.Tab("HUD Profile"):
                gr.Markdown(
                    "HUD profiles control context assembly/presentation. They are separate from the character's epistemic learning profile."
                )
                with gr.Row():
                    profile_name = gr.Textbox(label="Profile name", value="default")
                    load_profile = gr.Button("Load profile")
                    bind_profile = gr.Button("Bind to character")
                    list_profile_button = gr.Button("List profiles")
                profile_description = gr.Textbox(label="Description", lines=2)
                with gr.Row():
                    token_budget = gr.Number(label="HUD token budget", value=1600, precision=0)
                    recent_event_limit = gr.Number(label="Recent event limit", value=12, precision=0)
                    entity_hops = gr.Number(label="Scene entity hops", value=1, precision=0)
                with gr.Accordion("Retrieval controls (legacy HUD-owned)", open=False):
                    gr.Markdown("Retained for compatibility while retrieval ownership is being consolidated.")
                    semantic_retrieval_limit = gr.Number(label="Semantic retrieval limit", value=25, precision=0)
                    deep_memory_limit = gr.Number(label="Deep memory limit", value=0, precision=0)
                with gr.Row():
                    memory_budget = gr.Number(label="Memory budget", value=350, precision=0)
                    belief_budget = gr.Number(label="Belief budget", value=320, precision=0)
                    relationship_budget = gr.Number(label="Relationship budget", value=160, precision=0)
                with gr.Row():
                    scene_budget = gr.Number(label="Scene budget", value=260, precision=0)
                    inventory_budget = gr.Number(label="Inventory budget", value=140, precision=0)
                    rules_budget = gr.Number(label="Rules budget", value=140, precision=0)
                    goals_budget = gr.Number(label="Goals budget", value=140, precision=0)
                with gr.Row():
                    include_emotional_state = gr.Checkbox(label="Emotional state", value=True)
                    include_physical_state = gr.Checkbox(label="Physical state", value=True)
                    include_social_state = gr.Checkbox(label="Social state", value=True)
                    include_inventory = gr.Checkbox(label="Inventory", value=True)
                with gr.Row():
                    include_relationships = gr.Checkbox(label="Relationships", value=True)
                    include_conflicts = gr.Checkbox(label="Conflicts", value=True)
                    include_provenance = gr.Checkbox(label="Provenance", value=True)
                    include_confidence = gr.Checkbox(label="Confidence", value=True)
                save_hud_profile = gr.Button("Save HUD profile")
                profile_table = gr.Dataframe(
                    headers=["profile_name","token_budget","recent_events","memory","beliefs","scene","entity_hops"],
                    interactive=False, label="Available profiles",
                )

            with gr.Tab("Diagnostics"):
                with gr.Tabs():
                    with gr.Tab("Runtime / Worlds"):
                        runtime_table = gr.Dataframe(
                            headers=["instance_id","world_key","world_id","runtime_timeline","runtime_head",
                                     "source_timeline","source_head","lifecycle","state_version","controller_type","controller_ref"],
                            interactive=False, label="Character runtimes",
                        )
                    with gr.Tab("Scene State"):
                        scene_state = gr.JSON(label="Latest character-relative scene snapshot")
                    with gr.Tab("Speaker / Provenance"):
                        diagnostic_limit = gr.Number(label="Recent events", value=30, precision=0)
                        speaker_table = gr.Dataframe(
                            headers=["event_id","event_time","speaker_id","speaker_type","character_id",
                                     "viewpoint_id","session_id","node_id","timeline_id","message"],
                            interactive=False, label="Recent identity chain",
                        )

        identity_outputs = [
            character_overview, identity_kernel_text, identity_facets,
            identity_sources, identity_candidates, identity_revisions, legacy_identity,
        ]
        epistemic_outputs = [
            skepticism, curiosity, authority_trust, novelty_seeking,
            emotional_reactivity, retention, memory_continuity, source_trust,
            topic_interest, domain_expertise, trait_weights,
        ]
        hud_outputs = [
            profile_name, profile_description, token_budget, recent_event_limit,
            memory_budget, belief_budget, relationship_budget, scene_budget,
            inventory_budget, rules_budget, goals_budget, entity_hops,
            semantic_retrieval_limit, deep_memory_limit, include_emotional_state,
            include_physical_state, include_social_state, include_inventory,
            include_relationships, include_conflicts, include_provenance, include_confidence,
        ]

        def refresh_character_list():
            return gr.update(choices=run_async(_character_choices()))

        refresh_characters.click(fn=refresh_character_list, outputs=character_selector)

        def load_character_click(selection: str, diag_limit: float, research_count: float):
            cid = _character_id(selection)
            if not cid:
                raise gr.Error("Character is required")
            row = run_async(_load_character(cid))
            if not row:
                raise gr.Error(f"Character '{cid}' not found")
            epi = run_async(get_epistemic_profile(db, character_id=cid))
            hud = run_async(get_hud_profile(db, character_id=cid))
            overview, kernel_text, facets, sources, candidates, revisions = run_async(_identity_view(cid))
            legacy = {
                key: (list(value) if isinstance(value, tuple) else value)
                for key, value in dict(row).items()
                if key not in {"meta"}
            }
            identity = [overview, kernel_text, facets, sources, candidates, revisions, legacy]
            return (
                identity + _epistemic_values(epi) + _hud_values(hud)
                + [run_async(_domain_rows(cid)), run_async(_access_rows(cid)),
                   run_async(_research_rows(cid, int(research_count or 30))),
                   run_async(_runtime_rows(cid)), run_async(_scene_state(cid)),
                   run_async(_speaker_rows(cid, int(diag_limit or 30))),
                   f"Loaded **{cid}**."]
            )

        load_outputs = identity_outputs + epistemic_outputs + hud_outputs + [
            knowledge_domains, explicit_access, research_table, runtime_table,
            scene_state, speaker_table, status,
        ]
        load_character.click(
            fn=load_character_click,
            inputs=[character_selector, diagnostic_limit, research_limit],
            outputs=load_outputs,
        )

        def refresh_identity_components(cid):
            overview, kernel_text, facets, sources, candidates, revisions = run_async(_identity_view(cid))
            row = run_async(_load_character(cid))
            legacy = dict(row) if row else {}
            legacy.pop("meta", None)
            return overview, kernel_text, facets, sources, candidates, revisions, legacy

        def stage_facet_click(selection, facet_type, facet_key, value, stability,
                              mutability, perspective, authority):
            cid = _character_id(selection)
            if not cid:
                raise gr.Error("Character is required")
            try:
                result = run_async(_stage_manual_facet(
                    cid, facet_type, facet_key, value, stability, mutability,
                    perspective, authority,
                ))
            except Exception as exc:
                raise gr.Error(str(exc))
            return (*refresh_identity_components(cid),
                    f"Staged identity candidate **{result['candidate_ids'][0]}** for **{cid}**.")

        stage_facet.click(
            fn=stage_facet_click,
            inputs=[character_selector, new_facet_type, new_facet_key, new_facet_value,
                    new_facet_stability, new_facet_mutability, new_facet_perspective,
                    new_facet_authority],
            outputs=identity_outputs + [status],
        )

        def accept_candidate_click(selection, candidate, reason):
            cid = _character_id(selection)
            if not cid or not str(candidate or "").strip():
                raise gr.Error("Character and candidate ID are required")
            try:
                result = run_async(accept_identity_candidate(
                    db, str(candidate).strip(), actor="character-control",
                    reason=str(reason or "").strip() or None,
                ))
            except Exception as exc:
                raise gr.Error(str(exc))
            if result["character_id"] != cid:
                raise gr.Error("Candidate belongs to a different character")
            return (*refresh_identity_components(cid),
                    f"Accepted candidate. Identity is now version **{result['identity_version']}**.")

        accept_candidate.click(
            fn=accept_candidate_click,
            inputs=[character_selector, candidate_id, candidate_reason],
            outputs=identity_outputs + [status],
        )

        def reject_candidate_click(selection, candidate, reason):
            cid = _character_id(selection)
            if not cid or not str(candidate or "").strip():
                raise gr.Error("Character and candidate ID are required")
            snapshot = run_async(identity_snapshot(db, cid))
            match = next((r for r in snapshot["candidates"]
                          if str(r["candidate_id"]) == str(candidate).strip()), None)
            if not match:
                raise gr.Error("Candidate does not belong to the selected character")
            try:
                run_async(reject_identity_candidate(
                    db, str(candidate).strip(), reason=str(reason or "").strip() or None
                ))
            except Exception as exc:
                raise gr.Error(str(exc))
            return (*refresh_identity_components(cid), f"Rejected candidate for **{cid}**.")

        reject_candidate.click(
            fn=reject_candidate_click,
            inputs=[character_selector, candidate_id, candidate_reason],
            outputs=identity_outputs + [status],
        )

        def save_epistemics_click(selection, skepticism_v, curiosity_v, authority_v,
                                  novelty_v, reactivity_v, retention_v, continuity_v,
                                  source_rows, interest_rows, expertise_rows, trait_rows):
            cid = _character_id(selection)
            if not cid:
                raise gr.Error("Character is required")
            run_async(upsert_epistemic_profile(db, character_id=cid, data={
                "skepticism": skepticism_v, "curiosity": curiosity_v,
                "authority_trust": authority_v, "novelty_seeking": novelty_v,
                "emotional_reactivity": reactivity_v, "retention": retention_v,
                "source_trust": _rows_mapping(source_rows),
                "topic_interest": _rows_mapping(interest_rows),
                "domain_expertise": _rows_mapping(expertise_rows),
                "trait_weights": _rows_mapping(trait_rows),
            }))
            mode = str(continuity_v or "character").lower()
            if mode not in {"character","user","isolated"}:
                raise gr.Error("Invalid memory continuity")
            run_async(db.execute(
                "UPDATE aios.character_epistemic_profile SET memory_continuity=$2, updated_at=now() WHERE character_id=$1",
                cid, mode,
            ))
            return f"Saved epistemic and learning profile for **{cid}**."

        save_epistemics.click(
            fn=save_epistemics_click,
            inputs=[character_selector] + epistemic_outputs,
            outputs=status,
        )

        def save_domains_click(selection, rows):
            cid = _character_id(selection)
            if not cid:
                raise gr.Error("Character is required")
            result = run_async(_save_domains(cid, rows))
            return run_async(_domain_rows(cid)), result, f"Saved research affinity for **{cid}**."

        save_domains.click(
            fn=save_domains_click, inputs=[character_selector, knowledge_domains],
            outputs=[knowledge_domains, affinity_status, status],
        )

        def refresh_registered_domain_choices():
            return gr.update(choices=run_async(_registered_domain_choices()))

        refresh_registered_domains.click(
            fn=refresh_registered_domain_choices, outputs=registered_domain
        )

        def add_registered_domain_click(selection, domain_selection):
            cid = _character_id(selection)
            if not cid:
                raise gr.Error("Character is required")
            try:
                result = run_async(_add_character_domain(cid, domain_selection))
            except Exception as exc:
                raise gr.Error(str(exc))
            return run_async(_domain_rows(cid)), result, f"Added registered domain affinity for **{cid}**."

        add_registered_domain.click(
            fn=add_registered_domain_click,
            inputs=[character_selector, registered_domain],
            outputs=[knowledge_domains, affinity_status, status],
        )

        def load_profile_click(name):
            return _hud_values(run_async(get_profile_by_name(db, profile_name=name)))

        load_profile.click(fn=load_profile_click, inputs=profile_name, outputs=hud_outputs)

        def save_profile_click(name, description, token, recent, memory, beliefs, relationships,
                               scene, inventory, rules, goals, hops, semantic_limit, deep_limit,
                               emotion, physical, social, inventory_on, relationships_on,
                               conflicts, provenance, confidence):
            profile = run_async(save_profile(db, profile_name=name, description=description, values={
                "token_budget": token, "recent_event_limit": recent, "memory_budget": memory,
                "belief_budget": beliefs, "relationship_budget": relationships,
                "scene_budget": scene, "inventory_budget": inventory, "rules_budget": rules,
                "goals_budget": goals, "entity_hops": hops,
                "semantic_retrieval_limit": semantic_limit, "deep_memory_limit": deep_limit,
                "include_emotional_state": emotion, "include_physical_state": physical,
                "include_social_state": social, "include_inventory": inventory_on,
                "include_relationships": relationships_on, "include_conflicts": conflicts,
                "include_provenance": provenance, "include_confidence": confidence,
            }))
            return f"Saved HUD profile **{profile.profile_name}**."

        save_hud_profile.click(fn=save_profile_click, inputs=hud_outputs, outputs=status)

        def bind_profile_click(selection, name):
            cid = _character_id(selection)
            profile = run_async(bind_character_profile(db, character_id=cid, profile_name=name))
            return f"Bound **{cid}** to HUD profile **{profile.profile_name}**."

        bind_profile.click(fn=bind_profile_click, inputs=[character_selector, profile_name], outputs=status)

        def list_profiles_click():
            return [[p.profile_name,p.token_budget,p.recent_event_limit,p.memory_budget,
                     p.belief_budget,p.scene_budget,p.entity_hops] for p in run_async(list_profiles(db))]

        list_profile_button.click(fn=list_profiles_click, outputs=profile_table)

        def refresh_click(selection, diag_limit, research_count):
            cid = _character_id(selection)
            if not cid:
                raise gr.Error("Character is required")
            return (
                run_async(_runtime_rows(cid)), run_async(_scene_state(cid)),
                run_async(_speaker_rows(cid, int(diag_limit or 30))),
                run_async(_research_rows(cid, int(research_count or 30))),
                run_async(_access_rows(cid)),
                f"Refreshed diagnostics for **{cid}**.",
            )

        refresh_diagnostics.click(
            fn=refresh_click,
            inputs=[character_selector, diagnostic_limit, research_limit],
            outputs=[runtime_table, scene_state, speaker_table, research_table, explicit_access, status],
        )
