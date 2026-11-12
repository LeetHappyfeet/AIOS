"""Replay normalized chat evidence through the real SQL and RDF publishers."""
import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

from aios_app.db import Database
from aios_app.epistemic.episodes import materialize_event_occurrences_once
from aios_app.epistemic.topology_projection import project_semantic_scope
from aios_app.migrate import _migration_body
from aios_app.semantic_index.config import SemanticIndexConfig
from aios_app.semantic_index.eligibility import quarantine_ineligible_vectors_once
from aios_app.supervisor import STAGES
from test_cognitive_episode_postgres import PGConnection, PGPool

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not os.getenv("AIOS_PGLITE_MODULE"), reason="PGlite not configured")


@pytest_asyncio.fixture
async def db():
    process = await asyncio.create_subprocess_exec(
        "node", str(ROOT / "tests/cognition_pglite_bridge.mjs"),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
    )
    con = PGConnection(process)
    database = Database("isolated-scene-history")
    database.pool = PGPool(con)
    try:
        baseline = (ROOT / "aios_baseline.sql").read_text()
        # PGlite provides gen_random_uuid and sha256 through PostgreSQL core.
        await con.query("\n".join(line for line in baseline.splitlines() if not line.startswith(
            ("CREATE EXTENSION", "COMMENT ON EXTENSION")
        )), exec=True)
        await con.query("""
            CREATE FUNCTION aios.digest(data bytea, algorithm text) RETURNS bytea
            LANGUAGE sql IMMUTABLE AS 'SELECT sha256(data)'
        """, exec=True)
        for migration in sorted((ROOT / "migrations/current").glob("*.sql")):
            await con.query(_migration_body(migration.read_text(), migration.name), exec=True)
        yield database
    finally:
        process.stdin.close()
        await process.wait()


async def seed_turn(db, timeline, source_world, text, predicate, obj, *,
                    kind="EVENT", semantic_type="ACTION", quoted=False, subject="Cortana",
                    modality="asserted", polarity=1, admitted=True):
    event = await db.fetchval(
        "INSERT INTO aios.ingest_event(kind,dedupe_key) VALUES('chat_message',$1) RETURNING event_id",
        str(uuid4()),
    )
    node = await db.fetchval(
        """INSERT INTO aios.dag_node(timeline_id,event_id,kind,message_text)
           VALUES($1,$2,'chat_message',$3) RETURNING node_id""", timeline, event, text,
    )
    section = await db.fetchval(
        """INSERT INTO aios.document_section(section_path,section_order,content,node_id)
           VALUES($1,0,$2,$3) RETURNING section_id""", str(node), text, node,
    )
    sentence = await db.fetchval(
        """INSERT INTO aios.extracted_sentence(section_id,sentence_index,sentence_text)
           VALUES($1,0,$2) RETURNING sentence_id""", section, text,
    )
    claim = await db.fetchval(
        """INSERT INTO aios.claim_candidate(sentence_id,raw_text,extraction_ver)
           VALUES($1,$2,'fixture') RETURNING claim_id""", sentence, text,
    )
    proposition = await db.fetchval(
        """INSERT INTO aios.proposition(proposition_hash,topic_key,subject_norm,predicate_norm,
               object_norm,canonical_text,modality,polarity)
           VALUES($1,$2,$3,$4,$5,$6,$7,$8) RETURNING proposition_id""",
        str(uuid4()), predicate, subject.lower(), predicate, obj, text, modality, polarity,
    )
    await db.execute(
        """INSERT INTO aios.claim_context_resolution(claim_id,claim_kind,world_id,timeline_id,
               dag_node_id,epistemic_scope,resolver_version)
           VALUES($1,$2,$3,$4,$5,'narrative','fixture')""",
        claim, kind, source_world, timeline, node,
    )
    frame = await db.fetchval(
        """INSERT INTO aios.claim_semantic_frame(claim_id,frame_index,subject_text,resolved_subject,
               predicate_canonical,resolved_object,tense,resolution_status,decomposer_version,meta,modality,polarity)
           VALUES($1,0,$2,$3,$4,$5,'Past','resolved','semantic-frame-v2',$6::jsonb,$7,$8)
           RETURNING frame_id""",
        claim, "She", subject, predicate, obj, json.dumps({"inside_direct_quote": quoted}),
        modality, polarity,
    )
    await db.execute(
        """INSERT INTO aios.semantic_interpretation(claim_id,frame_id,semantic_type,
               standalone_semantic,interpreter_version)
           VALUES($1,$2,$3,true,'fixture')""", claim, frame, semantic_type,
    )
    observation = await db.fetchval(
        """INSERT INTO aios.observation(claim_id,proposition_id,timeline_id,dag_node_id,extraction_confidence)
           VALUES($1,$2,$3,$4,0.9) RETURNING observation_id""", claim, proposition, timeline, node,
    )
    await db.execute(
        """INSERT INTO aios.observation_proposition(observation_id,proposition_id,frame_id)
           VALUES($1,$2,$3)""", observation, proposition, frame,
    )
    await db.execute(
        """INSERT INTO aios.claim_semantic_integrity(claim_id,revision_key,validator_version,status,
               source_text,frame_snapshot)
           VALUES($1,'fixture','semantic-integrity-v3-fidelity','valid',$2,$3::jsonb)""",
        claim, text, json.dumps([{
            "frame_id": str(frame), "subject": subject, "predicate": predicate,
            "object": obj, "polarity": str(polarity), "modality": modality,
        }]),
    )
    if admitted:
        await db.execute(
            """INSERT INTO aios.semantic_exact_admission(claim_id,proposition_id,scope_key,decision,reason)
               VALUES($1,$2,'fixture','reinforces_exact','fixture')
               ON CONFLICT (claim_id) DO UPDATE SET decision=EXCLUDED.decision""", claim, proposition,
        )
    else:
        await db.execute("DELETE FROM aios.semantic_exact_admission WHERE claim_id=$1", claim)
    return claim, node, event


@pytest.mark.asyncio
async def test_scene_history_replay_branch_boundary_and_rdf_withdrawal(db):
    rdflib = pytest.importorskip("rdflib")
    rdf = rdflib.Dataset()

    class Fuseki:
        def update(self, dataset, query):
            assert dataset == "world"
            rdf.update(query)

    fuseki = Fuseki()
    world, sibling_world = uuid4(), uuid4()
    timeline, sibling_timeline, source_timeline = uuid4(), uuid4(), uuid4()
    source_world = await db.fetchval("INSERT INTO aios.world(world_key,world_type) VALUES('liminal','liminal') RETURNING world_id")
    await db.execute(
        "INSERT INTO aios.world(world_id,world_key,world_type) VALUES($1,'scene-test','runtime'),($2,'scene-sibling','runtime')",
        world, sibling_world,
    )
    await db.execute(
        """INSERT INTO aios.timeline(timeline_id,world_id,name)
           VALUES($1,$2,'scene-test'),($3,$4,'scene-sibling'),($5,$6,'scene-source')""",
        timeline, world, sibling_timeline, sibling_world, source_timeline, source_world,
    )
    tilt, tilt_node, tilt_event = await seed_turn(
        db, source_timeline, source_world, "She tilted her head.", "tilt", "her head",
    )
    sibling, sibling_node, _ = await seed_turn(
        db, source_timeline, source_world, "Cortana folded her arms.", "fold", "her arms",
    )
    movement, move_node, _ = await seed_turn(
        db, source_timeline, source_world, "Cortana entered the lab.", "enter", "the lab",
        kind="STATE", semantic_type="LOCATION",
    )
    walk, walk_node, _ = await seed_turn(
        db, source_timeline, source_world, "Cortana walked into the lab.", "walk", "the lab",
    )
    quoted, quote_node, _ = await seed_turn(
        db, source_timeline, source_world, 'Cortana said, "I entered the lab."', "enter", "the lab",
        kind="STATE", semantic_type="LOCATION", quoted=True,
    )
    _, future_node, _ = await seed_turn(
        db, source_timeline, source_world, "Cortana would tilt her head.", "tilt", "her head",
    )
    _, negative_node, _ = await seed_turn(
        db, source_timeline, source_world, "Cortana did not tilt her head.", "tilt", "her head",
        polarity=-1,
    )
    _, invalid_node, _ = await seed_turn(
        db, source_timeline, source_world, "Cortana lifted her hand.", "lift", "her hand",
        admitted=False,
    )
    main_nodes = [tilt_node, move_node, walk_node, quote_node, future_node, negative_node, invalid_node]
    for parent, child in zip(main_nodes, main_nodes[1:]):
        await db.execute(
            "INSERT INTO aios.dag_edge(timeline_id,parent_node_id,child_node_id) VALUES($1,$2,$3)",
            source_timeline, parent, child,
        )
    await db.execute("INSERT INTO aios.character_identity(character_id,display_name) VALUES('Cortana','Cortana')")
    actors = []
    for scene_world, scene_timeline, head in (
        (world, timeline, invalid_node), (sibling_world, sibling_timeline, sibling_node),
    ):
        instance = await db.fetchval(
            """INSERT INTO aios.character_instance(character_id,world_id,current_world_id)
               VALUES('Cortana',$1,$1) RETURNING instance_id""", scene_world,
        )
        await db.execute(
            """INSERT INTO aios.character_runtime_state(instance_id,world_id,timeline_id,
                   source_timeline_id,source_head_node_id) VALUES($1,$2,$3,$4,$5)""",
            instance, scene_world, scene_timeline, source_timeline, head,
        )
        actors.append(await db.fetchval(
            """INSERT INTO aios.world_entity(world_id,entity_key,entity_type,display_name,character_instance_id)
               VALUES($1,'cortana','character','Cortana',$2) RETURNING entity_id""", scene_world, instance,
        ))
        participant = await db.fetchval(
            """INSERT INTO aios.conversation_participant(timeline_id,character_instance_id,source_actor_id)
               VALUES($1,$2,$3) RETURNING participant_id""", source_timeline, instance, str(instance),
        )
        # Both have perception receipts. Only the adopted head's ancestry
        # determines which source occurrences enter each runtime graph.
        for node in [*main_nodes, sibling_node]:
            await db.execute(
                """INSERT INTO aios.message_participant(node_id,participant_id,relation)
                   VALUES($1,$2,'observer')""", node, participant,
            )

    lab = await db.fetchval(
        """INSERT INTO aios.world_entity(world_id,entity_key,entity_type,display_name)
           VALUES($1,'lab','location','Lab') RETURNING entity_id""", world,
    )
    stage = next(s for s in STAGES if s.name == "materialize_event_occurrences")
    eligible = {row["claim_id"] for row in await db.fetch(stage.eligibility_sql, 64)}
    assert {tilt, sibling, movement, walk} <= eligible
    assert quoted not in eligible
    assert await materialize_event_occurrences_once(db) >= 4
    assert await materialize_event_occurrences_once(db) == 0
    assert not await db.fetch(stage.eligibility_sql, 64)
    assert await db.fetchval("SELECT count(*) FROM aios.semantic_topology_node WHERE node_type='NARRATED_SCENE_EVENT'") == 4
    assert await db.fetchval("SELECT count(*) FROM aios.world_event") == 0
    occurrence = await db.fetchval(
        "SELECT node_key FROM aios.semantic_topology_node WHERE claim_id=$1", tilt,
    )
    await db.execute("DELETE FROM aios.semantic_topology_node WHERE claim_id=$1", tilt)
    assert [row["claim_id"] for row in await db.fetch(stage.eligibility_sql, 64)] == [tilt]
    assert await materialize_event_occurrences_once(db, claim_id=tilt) == 0

    async def publish(scene_world, scene_timeline):
        scope = f"world:{scene_world}:timeline:{scene_timeline}:narrated-events"
        await db.execute(
            """UPDATE aios.semantic_scope_projection_state
               SET dirty_at=now()-interval '10 minutes' WHERE scope_key=$1""", scope,
        )
        result = await project_semantic_scope(db, fuseki, scope_key=scope)
        assert result["projected"], result
        graph = await db.fetchval(
            "SELECT rdf_graph FROM aios.semantic_scope_projection_state WHERE scope_key=$1", scope,
        )
        return rdf.graph(rdflib.URIRef(graph))

    main_graph = await publish(world, timeline)
    sibling_graph = await publish(sibling_world, sibling_timeline)
    scene = rdflib.Namespace("urn:aios:scene#")
    tilt_uri = next(main_graph.subjects(scene.action, rdflib.Literal("tilt")))
    assert str(tilt_uri) == f"urn:aios:scene-event:{occurrence}"
    assert (tilt_uri, scene.actor, rdflib.URIRef(f"urn:aios:world-entity:{actors[0]}")) in main_graph
    assert (tilt_uri, scene.sourceNode, rdflib.URIRef(f"urn:aios:dag:node:{tilt_node}")) in main_graph
    assert set(main_graph.objects(None, scene.action)) == set(map(rdflib.Literal, ("tilt", "enter", "walk")))
    assert set(sibling_graph.objects(None, scene.action)) == {rdflib.Literal("fold")}
    moves = set(main_graph.subjects(scene.eventKind, rdflib.Literal("movement")))
    assert len(moves) == 2
    assert all((move, scene.object, rdflib.URIRef(f"urn:aios:world-entity:{lab}")) in main_graph for move in moves)

    await db.execute("UPDATE aios.ingest_event SET superseded_at=now() WHERE event_id=$1", tilt_event)
    await quarantine_ineligible_vectors_once(db, SemanticIndexConfig(), phase="topology_nodes")
    await publish(world, timeline)
    assert not list(main_graph.triples((tilt_uri, None, None)))
    assert not list(main_graph.triples((None, None, tilt_uri)))
    assert rdflib.Literal("enter") in set(main_graph.objects(None, scene.action))
    assert await db.fetchval("SELECT count(*) FROM aios.ingest_event WHERE event_id=$1", tilt_event) == 1
