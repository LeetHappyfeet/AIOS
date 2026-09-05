from __future__ import annotations

import logging
from uuid import UUID
from urllib.parse import quote

from aios_app.db import Database
from .fuseki import FusekiClient

logger = logging.getLogger("aios.rdf.epistemic_writer")

DATASET = "world"
GRAPH_IRI = "urn:aios:world:epistemic"
RECEIPT_PREDICATE = "world:observesProposition"


def _lit(value: str | None) -> str:
    if value is None:
        return '""'
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


async def project_normalized_observation(
    db: Database,
    fuseki: FusekiClient,
    *,
    claim_id: UUID,
) -> bool:
    row = await db.fetchrow(
        """
        SELECT
            o.observation_id, o.claim_id, o.source_key, o.source_domain,
            o.source_kind, o.observed_at, o.dag_node_id,
            NULLIF(o.meta->>'character_id', '') AS character_id,
            NULLIF(o.meta->>'memory_owner_id', '') AS memory_owner_id,
            NULLIF(o.meta->>'identity_ruleset', '') AS identity_ruleset,
            p.proposition_id, p.topic_key, p.canonical_text,
            p.subject_norm, p.predicate_norm, p.object_norm,
            p.polarity, p.modality
        FROM aios.observation o
        JOIN aios.proposition p ON p.proposition_id=o.proposition_id
        WHERE o.claim_id=$1
        """,
        claim_id,
    )
    if not row:
        raise RuntimeError(f"claim {claim_id} has not been normalized")

    world_receipt = await db.fetchrow(
        """
        SELECT 1 FROM aios.rdf_promotion_log
        WHERE claim_id=$1
          AND rdf_dataset=$2
          AND rdf_graph=$3
          AND rdf_predicate=$4
        """,
        claim_id,
        DATASET,
        GRAPH_IRI,
        RECEIPT_PREDICATE,
    )

    character_id = row["character_id"] or row["memory_owner_id"]
    char_graph = None
    char_receipt = None
    if character_id:
        char_graph = f"urn:aios:char:{quote(character_id, safe='')}:epistemic"
        char_receipt = await db.fetchrow(
            """
            SELECT 1 FROM aios.rdf_promotion_log
            WHERE claim_id=$1
              AND rdf_dataset='char'
              AND rdf_graph=$2
              AND rdf_predicate='char:knowsProposition'
            """,
            claim_id,
            char_graph,
        )

    if world_receipt and (not character_id or char_receipt):
        return True

    obs_iri = f"urn:aios:observation:{row['observation_id']}"
    prop_iri = f"urn:aios:proposition:{row['proposition_id']}"
    claim_iri = f"urn:aios:world:claim:{claim_id}"

    optional = []
    if row["subject_norm"]:
        optional.append(f"    world:normalizedSubject {_lit(row['subject_norm'])} ;")
    if row["predicate_norm"]:
        optional.append(f"    world:normalizedPredicate {_lit(row['predicate_norm'])} ;")
    if row["object_norm"]:
        optional.append(f"    world:normalizedObject {_lit(row['object_norm'])} ;")

    sparql = f"""
PREFIX world: <urn:aios:world#>
PREFIX prov:  <http://www.w3.org/ns/prov#>
PREFIX xsd:   <http://www.w3.org/2001/XMLSchema#>

INSERT DATA {{
  GRAPH <{GRAPH_IRI}> {{
    <{prop_iri}> a world:Proposition ;
      world:topicKey {_lit(row['topic_key'])} ;
      world:canonicalText {_lit(row['canonical_text'])} ;
      world:polarity "{int(row['polarity'])}"^^xsd:integer ;
      world:modality {_lit(row['modality'])} ;
{chr(10).join(optional)}
      prov:wasDerivedFrom <{claim_iri}> .

    <{obs_iri}> a world:Observation ;
      world:observesProposition <{prop_iri}> ;
      world:sourceKey {_lit(row['source_key'])} ;
      world:sourceDomain {_lit(row['source_domain'])} ;
      world:sourceKind {_lit(row['source_kind'])} ;
      world:observedAt "{row['observed_at'].isoformat()}"^^xsd:dateTime ;
      prov:wasDerivedFrom <{claim_iri}> .
  }}
}}
""".strip()

    if not world_receipt:
        fuseki.update(DATASET, sparql)

    if character_id and not char_receipt:
        char_dataset = "char"
        char_owner_segment = quote(character_id, safe="")
        char_graph = char_graph or f"urn:aios:char:{char_owner_segment}:epistemic"
        char_obs_iri = f"urn:aios:char:{char_owner_segment}:observation:{row['observation_id']}"
        char_sparql = f"""
PREFIX char:  <urn:aios:char#>
PREFIX world: <urn:aios:world#>
PREFIX prov:  <http://www.w3.org/ns/prov#>

INSERT DATA {{
  GRAPH <{char_graph}> {{
    <{char_obs_iri}> a char:CharacterObservation ;
      char:characterId {_lit(character_id)} ;
      char:memoryOwner {_lit(character_id)} ;
      char:identityRuleset {_lit(row["identity_ruleset"] or "character-id-v1")} ;
      char:knowsProposition <{prop_iri}> ;
      prov:wasDerivedFrom <{obs_iri}> .
  }}
}}
""".strip()
        fuseki.update(char_dataset, char_sparql)

        await db.execute(
            """
            INSERT INTO aios.rdf_promotion_log (
                claim_id, rdf_dataset, rdf_graph, rdf_subject,
                rdf_predicate, rdf_object, promoted_by, promotion_meta
            )
            VALUES ($1,$2,$3,$4,$5,$6,'epistemic_writer',$7::jsonb)
            ON CONFLICT (claim_id, rdf_dataset, rdf_graph, rdf_predicate) DO NOTHING
            """,
            claim_id,
            char_dataset,
            char_graph,
            char_obs_iri,
            "char:knowsProposition",
            prop_iri,
            '{"layer":"character-epistemic-v1","identity":"character_id"}',
        )

    if not world_receipt:
        await db.execute(
        """
        INSERT INTO aios.rdf_promotion_log (
            claim_id, rdf_dataset, rdf_graph, rdf_subject,
            rdf_predicate, rdf_object, promoted_by, promotion_meta
        )
        VALUES ($1,$2,$3,$4,$5,$6,'epistemic_writer',$7::jsonb)
        ON CONFLICT (claim_id, rdf_dataset, rdf_graph, rdf_predicate) DO NOTHING
        """,
        claim_id,
        DATASET,
        GRAPH_IRI,
        obs_iri,
        RECEIPT_PREDICATE,
        prop_iri,
        '{"layer":"normalized-observation-v1"}',
        )
    return True
