# aios_app/ui/tabs/character_editor.py

from __future__ import annotations

import asyncio
import threading
from typing import Any

import gradio as gr

from aios_app.config import settings
from aios_app.db import Database
from aios_app.hud.profile import (
    bind_character_profile,
    get_profile,
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


async def _load_character(character_id: str):
    return await db.fetchrow(
        "SELECT * FROM aios.character_identity WHERE character_id=$1",
        character_id,
    )


async def _save_character(character_id: str, values: dict[str, Any]) -> None:
    row = await db.fetchrow(
        "SELECT 1 FROM aios.character_identity WHERE character_id=$1",
        character_id,
    )
    if not row:
        raise ValueError(f"Unknown character_id '{character_id}'")

    await db.execute(
        """
        UPDATE aios.character_identity
        SET canonical_name=$2,
            display_name=$3,
            canon=$4,
            franchise=$5,
            entity_type=$6,
            species=$7,
            gender=$8,
            age_descriptor=$9,
            visual_summary=$10,
            primary_role=$11,
            archetype=$12,
            default_tone=$13,
            speech_style=$14,
            content_rating=$15,
            moral_constraints=$16,
            process_ontology=$17,
            is_canonical=$18,
            is_mutable=$19,
            updated_at=now()
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


async def _runtime_rows(character_id: str) -> list[list[Any]]:
    rows = await db.fetch(
        """
        SELECT
            ci.instance_id,
            w.world_key,
            rs.world_id,
            rs.timeline_id,
            rs.head_node_id,
            rs.source_timeline_id,
            rs.source_head_node_id,
            rs.lifecycle_state,
            rs.state_version,
            COALESCE(ec.controller_type, '') AS controller_type,
            COALESCE(ec.controller_ref, '') AS controller_ref
        FROM aios.character_instance ci
        LEFT JOIN aios.character_runtime_state rs
          ON rs.instance_id=ci.instance_id
        LEFT JOIN aios.world w
          ON w.world_id=rs.world_id
        LEFT JOIN LATERAL (
            SELECT controller_type, controller_ref
            FROM aios.world_entity we
            JOIN aios.entity_controller ec ON ec.entity_id=we.entity_id
            WHERE we.character_instance_id=ci.instance_id
              AND ec.active=true
            ORDER BY CASE ec.authority WHEN 'primary' THEN 0 ELSE 1 END,
                     ec.created_at
            LIMIT 1
        ) ec ON true
        WHERE ci.character_id=$1
        ORDER BY rs.updated_at DESC NULLS LAST, ci.created_at DESC
        """,
        character_id,
    )
    return [
        [
            str(r["instance_id"]),
            r["world_key"] or "",
            str(r["world_id"] or ""),
            str(r["timeline_id"] or ""),
            str(r["head_node_id"] or ""),
            str(r["source_timeline_id"] or ""),
            str(r["source_head_node_id"] or ""),
            r["lifecycle_state"] or "",
            r["state_version"] if r["state_version"] is not None else "",
            r["controller_type"],
            r["controller_ref"],
        ]
        for r in rows
    ]


async def _speaker_rows(character_id: str, limit: int = 30) -> list[list[Any]]:
    rows = await db.fetch(
        """
        SELECT
            ie.event_id,
            ie.event_time,
            ie.speaker_id,
            ie.speaker_role::text AS speaker_type,
            ie.character_id,
            ie.viewpoint_id,
            ie.session_id,
            dn.node_id,
            dn.timeline_id,
            left(COALESCE(ie.message_text,''), 180) AS message_text
        FROM aios.ingest_event ie
        LEFT JOIN aios.dag_node dn ON dn.event_id=ie.event_id
        WHERE ie.character_id=$1
        ORDER BY ie.event_id DESC
        LIMIT $2
        """,
        character_id,
        max(1, min(int(limit), 200)),
    )
    return [
        [
            r["event_id"],
            str(r["event_time"] or ""),
            r["speaker_id"] or "",
            r["speaker_type"] or "",
            r["character_id"] or "",
            r["viewpoint_id"] or "",
            str(r["session_id"] or ""),
            str(r["node_id"] or ""),
            str(r["timeline_id"] or ""),
            r["message_text"] or "",
        ]
        for r in rows
    ]


def _profile_values(profile) -> list[Any]:
    return [
        profile.profile_name,
        profile.description or "",
        profile.token_budget,
        profile.recent_event_limit,
        profile.memory_budget,
        profile.belief_budget,
        profile.relationship_budget,
        profile.scene_budget,
        profile.inventory_budget,
        profile.rules_budget,
        profile.goals_budget,
        profile.entity_hops,
        profile.semantic_retrieval_limit,
        profile.deep_memory_limit,
        profile.include_emotional_state,
        profile.include_physical_state,
        profile.include_social_state,
        profile.include_inventory,
        profile.include_relationships,
        profile.include_conflicts,
        profile.include_provenance,
        profile.include_confidence,
    ]


@register_tab
def render():
    with gr.Tab("Character Control"):
        gr.Markdown(
            """
            ### Character Control
            This UI edits durable character identity and HUD profile parameters.
            Runtime/world and speaker panels are read-only diagnostics. Backend
            processing decisions remain in AIOS, not in this Gradio layer.
            """
        )

        with gr.Row():
            character_id = gr.Textbox(
                label="character_id",
                placeholder="e.g. Natalie",
                scale=3,
            )
            load_character = gr.Button("Load character", scale=1)
            refresh_diagnostics = gr.Button("Refresh diagnostics", scale=1)

        status = gr.Markdown()

        with gr.Tabs():
            with gr.Tab("Identity"):
                with gr.Row():
                    canonical_name = gr.Textbox(label="Canonical name")
                    display_name = gr.Textbox(label="Display name")
                    entity_type = gr.Textbox(label="Entity type", value="character")

                with gr.Row():
                    canon = gr.Textbox(label="Canon")
                    franchise = gr.Textbox(label="Franchise")

                with gr.Row():
                    species = gr.Textbox(label="Species")
                    gender = gr.Textbox(label="Gender")
                    age_descriptor = gr.Textbox(label="Age descriptor")

                visual_summary = gr.Textbox(label="Visual summary", lines=3)

                with gr.Row():
                    primary_role = gr.Textbox(label="Primary role")
                    archetype = gr.Textbox(label="Archetype")
                    content_rating = gr.Textbox(label="Content rating", value="PG")

                default_tone = gr.CheckboxGroup(
                    label="Default tone",
                    choices=[
                        "calm", "playful", "sarcastic", "formal",
                        "aggressive", "empathetic", "curious",
                    ],
                )
                speech_style = gr.Textbox(label="Speech style", lines=2)
                moral_constraints = gr.Textbox(
                    label="Moral constraints (one per line)",
                    lines=4,
                )

                with gr.Row():
                    process_ontology = gr.Checkbox(label="Process ontology")
                    is_canonical = gr.Checkbox(label="Canonical identity", value=True)
                    is_mutable = gr.Checkbox(label="Identity mutable")

                save_identity = gr.Button("Save identity")

            with gr.Tab("HUD / Memory Profile"):
                gr.Markdown(
                    """
                    Profiles are backend configuration. The HUD assembler reads the
                    bound profile and decides how to apply these limits and feature
                    switches. Saving here does not perform retrieval or filtering.
                    """
                )

                with gr.Row():
                    profile_name = gr.Textbox(label="Profile name", value="default")
                    load_profile = gr.Button("Load profile")
                    bind_profile = gr.Button("Bind profile to character")
                    list_profile_button = gr.Button("List profiles")

                profile_description = gr.Textbox(label="Description", lines=2)

                with gr.Row():
                    token_budget = gr.Number(label="HUD token budget", value=1600, precision=0)
                    recent_event_limit = gr.Number(label="Recent event limit", value=12, precision=0)
                    entity_hops = gr.Number(label="Scene entity hops", value=1, precision=0)

                with gr.Row():
                    semantic_retrieval_limit = gr.Number(
                        label="Semantic retrieval limit", value=25, precision=0
                    )
                    deep_memory_limit = gr.Number(
                        label="Deep memory limit", value=0, precision=0
                    )

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
                    include_emotional_state = gr.Checkbox(label="Include emotional state", value=True)
                    include_physical_state = gr.Checkbox(label="Include physical state", value=True)
                    include_social_state = gr.Checkbox(label="Include social state", value=True)
                    include_inventory = gr.Checkbox(label="Include inventory", value=True)

                with gr.Row():
                    include_relationships = gr.Checkbox(label="Include relationships", value=True)
                    include_conflicts = gr.Checkbox(label="Include conflicts", value=True)
                    include_provenance = gr.Checkbox(label="Include provenance", value=True)
                    include_confidence = gr.Checkbox(label="Include confidence", value=True)

                save_hud_profile = gr.Button("Save profile")

                profile_table = gr.Dataframe(
                    headers=[
                        "profile_name", "token_budget", "recent_events",
                        "memory", "beliefs", "scene", "entity_hops",
                    ],
                    interactive=False,
                    label="Available profiles",
                )

            with gr.Tab("Runtime / Worlds"):
                gr.Markdown(
                    "Read-only runtime topology. These values are produced by the backend."
                )
                runtime_table = gr.Dataframe(
                    headers=[
                        "instance_id", "world_key", "world_id",
                        "runtime_timeline", "runtime_head",
                        "source_timeline", "source_head",
                        "lifecycle", "state_version",
                        "controller_type", "controller_ref",
                    ],
                    interactive=False,
                    label="Character runtimes",
                )

            with gr.Tab("Speaker Diagnostics"):
                gr.Markdown(
                    """
                    Read-only identity provenance. speaker_id is who produced the
                    event; character_id is the active character context;
                    viewpoint_id controls first-person resolution.
                    """
                )
                diagnostic_limit = gr.Number(label="Recent events", value=30, precision=0)
                speaker_table = gr.Dataframe(
                    headers=[
                        "event_id", "event_time", "speaker_id", "speaker_type",
                        "character_id", "viewpoint_id", "session_id",
                        "node_id", "timeline_id", "message",
                    ],
                    interactive=False,
                    label="Recent identity chain",
                )

        identity_outputs = [
            canonical_name, display_name, entity_type, canon, franchise,
            species, gender, age_descriptor, visual_summary, primary_role,
            archetype, default_tone, speech_style, content_rating,
            moral_constraints, process_ontology, is_canonical, is_mutable,
            profile_name, profile_description, token_budget, recent_event_limit,
            memory_budget, belief_budget, relationship_budget, scene_budget,
            inventory_budget, rules_budget, goals_budget, entity_hops,
            semantic_retrieval_limit, deep_memory_limit,
            include_emotional_state, include_physical_state,
            include_social_state, include_inventory, include_relationships,
            include_conflicts, include_provenance, include_confidence,
            runtime_table, speaker_table, status,
        ]

        def load_character_click(cid: str, diag_limit: float):
            if not cid:
                raise gr.Error("character_id is required")
            row = run_async(_load_character(cid))
            if not row:
                raise gr.Error(f"Character '{cid}' not found")

            profile = run_async(get_profile(db, character_id=cid))
            runtime = run_async(_runtime_rows(cid))
            speakers = run_async(_speaker_rows(cid, int(diag_limit or 30)))

            identity = [
                row["canonical_name"] or "",
                row["display_name"] or "",
                row["entity_type"] or "character",
                row["canon"] or "",
                row["franchise"] or "",
                row["species"] or "",
                row["gender"] or "",
                row["age_descriptor"] or "",
                row["visual_summary"] or "",
                row["primary_role"] or "",
                row["archetype"] or "",
                row["default_tone"] or [],
                row["speech_style"] or "",
                row["content_rating"] or "PG",
                "\n".join(row["moral_constraints"] or []),
                bool(row["process_ontology"]),
                bool(row["is_canonical"]),
                bool(row["is_mutable"]),
            ]
            return identity + _profile_values(profile) + [
                runtime,
                speakers,
                f"Loaded **{cid}** with HUD profile **{profile.profile_name}**.",
            ]

        load_character.click(
            fn=load_character_click,
            inputs=[character_id, diagnostic_limit],
            outputs=identity_outputs,
        )

        def save_identity_click(
            cid, canonical, display, entity, canon_value, franchise_value,
            species_value, gender_value, age_value, visual, role, archetype_value,
            tones, speech, rating, constraints, ontology, canonical_flag, mutable,
        ):
            if not cid:
                raise gr.Error("character_id is required")
            values = {
                "canonical_name": canonical,
                "display_name": display,
                "entity_type": entity,
                "canon": canon_value,
                "franchise": franchise_value,
                "species": species_value,
                "gender": gender_value,
                "age_descriptor": age_value,
                "visual_summary": visual,
                "primary_role": role,
                "archetype": archetype_value,
                "default_tone": tones or [],
                "speech_style": speech,
                "content_rating": rating,
                "moral_constraints": [
                    line.strip() for line in (constraints or "").splitlines()
                    if line.strip()
                ],
                "process_ontology": ontology,
                "is_canonical": canonical_flag,
                "is_mutable": mutable,
            }
            run_async(_save_character(cid, values))
            return f"Saved identity parameters for **{cid}**."

        save_identity.click(
            fn=save_identity_click,
            inputs=[
                character_id, canonical_name, display_name, entity_type,
                canon, franchise, species, gender, age_descriptor,
                visual_summary, primary_role, archetype, default_tone,
                speech_style, content_rating, moral_constraints,
                process_ontology, is_canonical, is_mutable,
            ],
            outputs=status,
        )

        profile_outputs = [
            profile_name, profile_description, token_budget, recent_event_limit,
            memory_budget, belief_budget, relationship_budget, scene_budget,
            inventory_budget, rules_budget, goals_budget, entity_hops,
            semantic_retrieval_limit, deep_memory_limit,
            include_emotional_state, include_physical_state,
            include_social_state, include_inventory, include_relationships,
            include_conflicts, include_provenance, include_confidence,
        ]

        def load_profile_click(name: str):
            if not name:
                raise gr.Error("Profile name is required")
            profile = run_async(get_profile_by_name(db, profile_name=name))
            return _profile_values(profile)

        load_profile.click(
            fn=load_profile_click,
            inputs=profile_name,
            outputs=profile_outputs,
        )

        def save_profile_click(
            name, description, token, recent, memory, beliefs, relationships,
            scene, inventory, rules, goals, hops, semantic_limit, deep_limit,
            emotion, physical, social,
            inventory_on, relationships_on, conflicts, provenance, confidence,
        ):
            if not name:
                raise gr.Error("Profile name is required")
            profile = run_async(
                save_profile(
                    db,
                    profile_name=name,
                    description=description,
                    values={
                        "token_budget": token,
                        "recent_event_limit": recent,
                        "memory_budget": memory,
                        "belief_budget": beliefs,
                        "relationship_budget": relationships,
                        "scene_budget": scene,
                        "inventory_budget": inventory,
                        "rules_budget": rules,
                        "goals_budget": goals,
                        "entity_hops": hops,
                        "semantic_retrieval_limit": semantic_limit,
                        "deep_memory_limit": deep_limit,
                        "include_emotional_state": emotion,
                        "include_physical_state": physical,
                        "include_social_state": social,
                        "include_inventory": inventory_on,
                        "include_relationships": relationships_on,
                        "include_conflicts": conflicts,
                        "include_provenance": provenance,
                        "include_confidence": confidence,
                    },
                )
            )
            return f"Saved HUD profile **{profile.profile_name}**."

        save_hud_profile.click(
            fn=save_profile_click,
            inputs=profile_outputs,
            outputs=status,
        )

        def bind_profile_click(cid: str, name: str):
            if not cid or not name:
                raise gr.Error("character_id and profile name are required")
            profile = run_async(
                bind_character_profile(db, character_id=cid, profile_name=name)
            )
            return f"Bound **{cid}** to HUD profile **{profile.profile_name}**."

        bind_profile.click(
            fn=bind_profile_click,
            inputs=[character_id, profile_name],
            outputs=status,
        )

        def list_profiles_click():
            profiles = run_async(list_profiles(db))
            return [
                [
                    p.profile_name, p.token_budget, p.recent_event_limit,
                    p.memory_budget, p.belief_budget, p.scene_budget,
                    p.entity_hops,
                ]
                for p in profiles
            ]

        list_profile_button.click(
            fn=list_profiles_click,
            outputs=profile_table,
        )

        def refresh_click(cid: str, diag_limit: float):
            if not cid:
                raise gr.Error("character_id is required")
            return (
                run_async(_runtime_rows(cid)),
                run_async(_speaker_rows(cid, int(diag_limit or 30))),
                f"Refreshed diagnostics for **{cid}**.",
            )

        refresh_diagnostics.click(
            fn=refresh_click,
            inputs=[character_id, diagnostic_limit],
            outputs=[runtime_table, speaker_table, status],
        )
