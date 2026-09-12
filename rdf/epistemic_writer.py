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
BELIEF_RDF_VERSION = "character-belief-rdf-v1"


def _lit(value: str | None) -> str:
    if value is None:
        return '""'
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _optional_atom_fields(row) -> list[str]:
    fields: list[str] = []
    if row["subject_norm"]:
        fields.append(f"      char:normalizedSubject {_lit(row['subject_norm'])} ;")
    if row["predicate_norm"]:
        fields.append(f"      char:normalizedPredicate {_lit(row['predicate_norm'])} ;")
    if row["object_norm"]:
        fields.append(f"      char:normalizedObject {_lit(row['object_norm'])} ;")
    return fields


async def project_character_belief_state(
    db: Database,
    fuseki: FusekiClient,
    *,
    instance_id: UUID,
) -> bool:
    """Synchronize one reconciled /char belief state into character RDF.

    RDF mirrors character_belief_state, not raw acquisition evidence.  Positive,
    negative, and unresolved stances are explicit resources over the same
    polarity-independent semantic atom.  Evidence/provenance remains available
    through CharacterObservation and the SQL/RDF acquisition topology.
    """

    owner = await db.fetchrow(
        """
        SELECT ci.character_id
        FROM aios.character_instance ci
        WHERE ci.instance_id=$1
        """,
        instance_id,
    )
    if not owner or not owner["character_id"]:
        return False

    character_id = str(owner["character_id"])
    char_owner_segment = quote(character_id, safe="")
    char_graph = f"urn:aios:char:{char_owner_segment}:epistemic"
    char_iri = f"urn:aios:char:{char_owner_segment}"
    instance_iri = f"urn:aios:character-instance:{instance_id}"

    rows = await db.fetch(
        """
        SELECT
            bs.instance_id,
            bs.atom_id,
            bs.stance,
            bs.positive_support,
            bs.negative_support,
            bs.belief_confidence,
            bs.preferred_proposition_id,
            bs.evidence_count,
            bs.independent_evidence_count,
            bs.resolved_through_node_id,
            bs.resolver_version,
            bs.resolved_at,
            a.subject_norm,
            a.predicate_norm,
            a.object_norm
        FROM aios.character_belief_state bs
        JOIN aios.semantic_atom a ON a.atom_id=bs.atom_id
        WHERE bs.instance_id=$1
        ORDER BY bs.belief_confidence DESC, bs.atom_id
        """,
        instance_id,
    )

    # Replace only this instance's convergent belief-state resources.  Raw
    # observations and semantic atoms from other instances are untouched.
    cleanup = f"""
PREFIX char: <urn:aios:char#>
DELETE {{
  GRAPH <{char_graph}> {{
    <{char_iri}> char:hasBeliefState ?belief .
    ?belief ?p ?o .
  }}
}}
WHERE {{
  GRAPH <{char_graph}> {{
    <{char_iri}> char:hasBeliefState ?belief .
    ?belief char:characterInstance <{instance_iri}> ;
            ?p ?o .
  }}
}}
""".strip()
    fuseki.update("char", cleanup)

    if not rows:
        return True

    triples: list[str] = [
        f"    <{char_iri}> a char:Character ;",
        f"      char:characterId {_lit(character_id)} .",
    ]

    for row in rows:
        belief_iri = (
            f"urn:aios:char:{char_owner_segment}:instance:{instance_id}:"
            f"belief:{row['atom_id']}"
        )
        atom_iri = f"urn:aios:semantic-atom:{row['atom_id']}"
        stance = str(row["stance"])
        polarity = 1 if stance == "positive" else -1 if stance == "negative" else 0

        triples.extend(
            [
                f"    <{char_iri}> char:hasBeliefState <{belief_iri}> .",
                f"    <{belief_iri}> a char:BeliefState ;",
                f"      char:characterInstance <{instance_iri}> ;",
                f"      char:semanticAtom <{atom_iri}> ;",
                f"      char:stance {_lit(stance)} ;",
                f"      char:assertedPolarity \"{polarity}\"^^xsd:integer ;",
                f"      char:positiveSupport \"{float(row['positive_support']):.12g}\"^^xsd:double ;",
                f"      char:negativeSupport \"{float(row['negative_support']):.12g}\"^^xsd:double ;",
                f"      char:beliefConfidence \"{float(row['belief_confidence']):.12g}\"^^xsd:double ;",
                f"      char:evidenceCount \"{int(row['evidence_count'])}\"^^xsd:integer ;",
                f"      char:independentEvidenceCount \"{int(row['independent_evidence_count'])}\"^^xsd:integer ;",
                f"      char:resolverVersion {_lit(row['resolver_version'])} ;",
                f"      char:rdfProjectionVersion {_lit(BELIEF_RDF_VERSION)} ;",
                f"      char:resolvedAt \"{row['resolved_at'].isoformat()}\"^^xsd:dateTime"
                + (
                    f" ;\n      char:preferredProposition <urn:aios:proposition:{row['preferred_proposition_id']}>"
                    if row["preferred_proposition_id"]
                    else ""
                )
                + (
                    f" ;\n      char:resolvedThroughNode <urn:aios:dag-node:{row['resolved_through_node_id']}>"
                    if row["resolved_through_node_id"]
                    else ""
                )
                + " .",
                f"    <{atom_iri}> a char:SemanticAtom ;",
                f"      char:atomId {_lit(str(row['atom_id']))} ;",
                *_optional_atom_fields(row),
                f"      char:polarityIndependent \"true\"^^xsd:boolean .",
            ]
        )

    belief_sparql = f"""
PREFIX char: <urn:aios:char#>
PREFIX xsd:  <http://www.w3.org/2001/XMLSchema#>
INSERT DATA {{
  GRAPH <{char_graph}> {{
{chr(10).join(triples)}
  }}
}}
""".strip()
    fuseki.update("char", belief_sparql)
    return True


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
            ccr.origin_character_id AS character_id,
            ccr.origin_character_id AS memory_owner_id,
            ccr.character_instance_id,
            ccr.world_id,
            ccr.viewpoint_id,
            ccr.epistemic_scope,
            ccr.acquisition_mode,
            ccr.claim_kind,
            ccr.subject_kind,
            ccr.object_kind,
            ccr.predicate_family,
            ccr.subject_is_pivot,
            ccr.object_is_pivot,
            NULLIF(o.meta->>'identity_ruleset', '') AS identity_ruleset,
            p.proposition_id, p.topic_key, p.canonical_text,
            p.subject_norm, p.predicate_norm, p.object_norm,
            p.polarity, p.modality
        FROM aios.observation o
        JOIN aios.proposition p ON p.proposition_id=o.proposition_id
        JOIN aios.claim_context_resolution ccr ON ccr.claim_id=o.claim_id
        WHERE o.claim_id=$1
        """,
        claim_id,
    )
    if not row:
        raise RuntimeError(f"claim {claim_id} has not been normalized")

    perceiver_rows = await db.fetch(
        """
        SELECT DISTINCT kae.instance_id
        FROM aios.knowledge_acquisition_event kae
        WHERE kae.claim_id=$1
          AND kae.instance_id IS NOT NULL
        """,
        claim_id,
    )

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
              AND rdf_predicate='char:hasObservation'
            """,
            claim_id,
            char_graph,
        )

    if world_receipt and (not character_id or char_receipt):
        for perceiver in perceiver_rows:
            await project_character_belief_state(
                db,
                fuseki,
                instance_id=perceiver["instance_id"],
            )
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
      world:indexScope "system-observation-index" ;
      world:epistemicScope {_lit(row['epistemic_scope'])} ;
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
        context_links = []
        if row["character_instance_id"]:
            context_links.append(
                f"<{char_obs_iri}> char:characterInstance "
                f"<urn:aios:character-instance:{row['character_instance_id']}> ."
            )
        if row["world_id"]:
            context_links.append(
                f"<{char_obs_iri}> char:originWorld "
                f"<urn:aios:world:{row['world_id']}> ."
            )

        char_sparql = f"""
PREFIX char:  <urn:aios:char#>
PREFIX world: <urn:aios:world#>
PREFIX prov:  <http://www.w3.org/ns/prov#>

INSERT DATA {{
  GRAPH <{char_graph}> {{
    <urn:aios:char:{char_owner_segment}> a char:Character ;
      char:characterId {_lit(character_id)} ;
      char:hasObservation <{char_obs_iri}> .

    <{char_obs_iri}> a char:CharacterObservation ;
      char:characterId {_lit(character_id)} ;
      char:memoryOwner {_lit(character_id)} ;
      char:viewpointId {_lit(row["viewpoint_id"])} ;
      char:epistemicScope {_lit(row["epistemic_scope"])} ;
      char:acquisitionMode {_lit(row["acquisition_mode"])} ;
      char:claimKind {_lit(row["claim_kind"])} ;
      char:predicateFamily {_lit(row["predicate_family"])} ;
      char:identityRuleset {_lit(row["identity_ruleset"] or "character-id-v1")} ;
      char:observesProposition <{prop_iri}> ;
      prov:wasDerivedFrom <{obs_iri}> .

    {chr(10).join(context_links)}
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
            "char:hasObservation",
            char_obs_iri,
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

    for perceiver in perceiver_rows:
        await project_character_belief_state(
            db,
            fuseki,
            instance_id=perceiver["instance_id"],
        )

    return True
