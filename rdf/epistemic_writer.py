from __future__ import annotations

import hashlib
import json
import logging
from uuid import UUID
from urllib.parse import quote

from aios_app.db import Database
from .fuseki import FusekiClient

logger = logging.getLogger("aios.rdf.epistemic_writer")

DATASET = "world"
GRAPH_IRI = "urn:aios:world:epistemic"
RECEIPT_PREDICATE = "world:observesProposition"
BELIEF_RDF_VERSION = "character-belief-rdf-v2"
EPISTEMIC_RDF_VERSION = "epistemic-authority-rdf-v1"
BELIEF_RDF_BATCH_SIZE = 128


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


def _belief_projection_hash(row) -> str:
    """Fingerprint only RDF-visible semantic state, not reconciliation timestamps."""

    payload = {
        "atom_id": str(row["belief_atom_id"]),
        "stance": str(row["stance"]),
        "positive_support": float(row["positive_support"]),
        "negative_support": float(row["negative_support"]),
        "belief_confidence": float(row["belief_confidence"]),
        "preferred_proposition_id": (
            str(row["preferred_proposition_id"])
            if row["preferred_proposition_id"]
            else None
        ),
        "evidence_count": int(row["evidence_count"]),
        "independent_evidence_count": int(row["independent_evidence_count"]),
        "resolved_through_node_id": (
            str(row["resolved_through_node_id"])
            if row["resolved_through_node_id"]
            else None
        ),
        "resolver_version": str(row["resolver_version"]),
        "subject_norm": row["subject_norm"],
        "predicate_norm": row["predicate_norm"],
        "object_norm": row["object_norm"],
        "rdf_version": BELIEF_RDF_VERSION,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _belief_iris(*, character_id: str, instance_id: UUID, atom_id: UUID) -> tuple[str, str]:
    owner_segment = quote(character_id, safe="")
    belief_iri = (
        f"urn:aios:char:{owner_segment}:instance:{instance_id}:belief:{atom_id}"
    )
    atom_iri = f"urn:aios:semantic-atom:{atom_id}"
    return belief_iri, atom_iri


def _belief_triples(
    row,
    *,
    character_id: str,
    instance_id: UUID,
    char_iri: str,
    instance_iri: str,
) -> list[str]:
    atom_id = row["belief_atom_id"]
    belief_iri, atom_iri = _belief_iris(
        character_id=character_id,
        instance_id=instance_id,
        atom_id=atom_id,
    )
    stance = str(row["stance"])
    polarity = 1 if stance == "positive" else -1 if stance == "negative" else 0

    triples = [
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
        f"      char:rdfProjectionVersion {_lit(BELIEF_RDF_VERSION)}"
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
        f"      char:atomId {_lit(str(atom_id))} ;",
        *_optional_atom_fields(row),
        f"      char:polarityIndependent \"true\"^^xsd:boolean .",
    ]
    return triples


async def _ack_belief_projection(
    db: Database,
    *,
    instance_id: UUID,
    rows: list[dict],
) -> None:
    """Commit projection receipts without losing mutations that raced the RDF write."""

    async with db.connection() as con:
        async with con.transaction():
            for item in rows:
                atom_id = item["atom_id"]
                projection_hash = item.get("projection_hash")
                if projection_hash is None:
                    await con.execute(
                        """
                        DELETE FROM aios.rdf_character_belief_projection
                        WHERE instance_id=$1 AND atom_id=$2
                        """,
                        instance_id,
                        atom_id,
                    )
                elif item.get("wrote_rdf"):
                    await con.execute(
                        """
                        INSERT INTO aios.rdf_character_belief_projection (
                            instance_id, atom_id, projection_hash,
                            projection_version, projected_at
                        )
                        VALUES ($1,$2,$3,$4,now())
                        ON CONFLICT (instance_id, atom_id) DO UPDATE
                        SET projection_hash=EXCLUDED.projection_hash,
                            projection_version=EXCLUDED.projection_version,
                            projected_at=now()
                        """,
                        instance_id,
                        atom_id,
                        projection_hash,
                        BELIEF_RDF_VERSION,
                    )

                # A concurrent trigger increments dirty_version. In that case this
                # DELETE intentionally misses and the newer state is projected next.
                await con.execute(
                    """
                    DELETE FROM aios.rdf_character_belief_dirty
                    WHERE instance_id=$1
                      AND atom_id=$2
                      AND dirty_version=$3
                    """,
                    instance_id,
                    atom_id,
                    item["dirty_version"],
                )


async def project_character_belief_state(
    db: Database,
    fuseki: FusekiClient,
    *,
    instance_id: UUID,
) -> bool:
    """Incrementally materialize reconciled /char belief state into Fuseki.

    PostgreSQL is authoritative. The dirty table is a coalescing outbox keyed by
    (instance_id, atom_id), so repeated reconciliation of the same atom becomes
    one RDF mutation. Unchanged fingerprints perform zero Fuseki writes.
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

    dirty_rows = await db.fetch(
        """
        SELECT
            d.atom_id AS dirty_atom_id,
            d.dirty_version,
            rp.projection_hash AS previous_hash,
            bs.atom_id AS belief_atom_id,
            bs.stance,
            bs.positive_support,
            bs.negative_support,
            bs.belief_confidence,
            bs.preferred_proposition_id,
            bs.evidence_count,
            bs.independent_evidence_count,
            bs.resolved_through_node_id,
            bs.resolver_version,
            a.subject_norm,
            a.predicate_norm,
            a.object_norm
        FROM aios.rdf_character_belief_dirty d
        LEFT JOIN aios.character_belief_state bs
          ON bs.instance_id=d.instance_id
         AND bs.atom_id=d.atom_id
        LEFT JOIN aios.semantic_atom a
          ON a.atom_id=bs.atom_id
        LEFT JOIN aios.rdf_character_belief_projection rp
          ON rp.instance_id=d.instance_id
         AND rp.atom_id=d.atom_id
        WHERE d.instance_id=$1
        ORDER BY d.dirty_at, d.atom_id
        LIMIT $2
        """,
        instance_id,
        BELIEF_RDF_BATCH_SIZE,
    )
    if not dirty_rows:
        return True

    pending: list[dict] = []
    delete_ops: list[str] = []
    insert_triples: list[str] = []

    for row in dirty_rows:
        atom_id = row["dirty_atom_id"]
        belief_iri, atom_iri = _belief_iris(
            character_id=character_id,
            instance_id=instance_id,
            atom_id=atom_id,
        )
        exists = row["belief_atom_id"] is not None
        projection_hash = _belief_projection_hash(row) if exists else None
        previous_hash = row["previous_hash"]
        changed = (projection_hash != previous_hash) if exists else previous_hash is not None

        item = {
            "atom_id": atom_id,
            "dirty_version": int(row["dirty_version"]),
            "projection_hash": projection_hash,
            "wrote_rdf": False,
        }
        pending.append(item)

        if not changed:
            continue

        delete_ops.append(
            f"DELETE WHERE {{ GRAPH <{char_graph}> {{ "
            f"<{char_iri}> char:hasBeliefState <{belief_iri}> . }} }}"
        )
        delete_ops.append(
            f"DELETE WHERE {{ GRAPH <{char_graph}> {{ <{belief_iri}> ?p ?o . }} }}"
        )

        if exists:
            # SemanticAtom is a shared stable resource. Replacing its properties
            # prevents stale normalized fields without rebuilding the graph.
            delete_ops.append(
                f"DELETE WHERE {{ GRAPH <{char_graph}> {{ <{atom_iri}> ?ap ?ao . }} }}"
            )
            insert_triples.extend(
                _belief_triples(
                    row,
                    character_id=character_id,
                    instance_id=instance_id,
                    char_iri=char_iri,
                    instance_iri=instance_iri,
                )
            )

        item["wrote_rdf"] = True

    if delete_ops or insert_triples:
        operations = [
            "PREFIX char: <urn:aios:char#>\n" + op
            for op in delete_ops
        ]
        if insert_triples:
            operations.append(
                "PREFIX char: <urn:aios:char#>\n"
                "PREFIX xsd:  <http://www.w3.org/2001/XMLSchema#>\n"
                f"INSERT DATA {{ GRAPH <{char_graph}> {{\n"
                + "\n".join(insert_triples)
                + "\n} }"
            )
        # One HTTP UpdateRequest = one bounded TDB2 write transaction for the
        # coalesced belief batch, rather than DELETE+INSERT for the whole graph.
        fuseki.update("char", ";\n".join(operations))

    await _ack_belief_projection(
        db,
        instance_id=instance_id,
        rows=pending,
    )

    logger.debug(
        "Projected character belief delta instance=%s dirty=%s rdf_changed=%s",
        instance_id,
        len(pending),
        sum(1 for item in pending if item["wrote_rdf"]),
    )
    return True


def _authority_insert_lines(prefix: str, authority) -> list[str]:
    """Render SQL authority admission as RDF-visible control-plane metadata."""
    if not authority:
        return []
    lines = [
        f"      {prefix}:originKind {_lit(authority['origin_kind'])} ;",
        f"      {prefix}:epistemicMode {_lit(authority['epistemic_mode'])} ;",
        f"      {prefix}:authorityState {_lit(authority['authority_state'])} ;",
        f"      {prefix}:authorityRank \"{int(authority['authority_rank'])}\"^^xsd:integer ;",
        f"      {prefix}:lineageKey {_lit(authority['lineage_key'])} ;",
        f"      {prefix}:predicateClass {_lit(authority['predicate_class'])} ;",
        f"      {prefix}:authorityPolicyVersion {_lit(authority['policy_version'])} ;",
        f"      {prefix}:rdfProjectionVersion {_lit(EPISTEMIC_RDF_VERSION)} ;",
    ]
    for use in authority["authorized_uses"] or []:
        lines.append(f"      {prefix}:authorizedUse {_lit(str(use))} ;")
    return lines


async def _project_observation_authority(
    db: Database,
    fuseki: FusekiClient,
    *,
    dataset: str,
    graph_iri: str,
    observation_iri: str,
    prefix: str,
    namespace: str,
    authority,
) -> None:
    """Replace only authority predicates so old projection receipts can be upgraded."""
    if not authority:
        return

    # The receipt describes the last successfully replicated RDF-visible
    # authority state. It deliberately excludes reconciliation timestamps.
    fingerprint = hashlib.sha256(
        "\n".join(_authority_insert_lines(prefix, authority)).encode("utf-8")
    ).hexdigest()
    previous = await db.fetchrow(
        """
        SELECT projection_hash
        FROM aios.rdf_observation_authority_projection
        WHERE rdf_dataset=$1 AND rdf_graph=$2 AND observation_iri=$3
        """,
        dataset, graph_iri, observation_iri,
    )
    if previous and previous["projection_hash"] == fingerprint:
        return

    predicates = (
        "originKind", "epistemicMode", "authorityState", "authorityRank",
        "lineageKey", "predicateClass", "authorityPolicyVersion",
        "authorizedUse", "rdfProjectionVersion",
    )
    inserts = "\n".join(_authority_insert_lines(prefix, authority))
    # _authority_insert_lines is semicolon-oriented; terminate the compact
    # property list by replacing the final semicolon with a period.
    inserts = inserts.rstrip()
    if inserts.endswith(";"):
        inserts = inserts[:-1] + " ."

    sparql = f"""
PREFIX {prefix}: <{namespace}>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
DELETE {{
  GRAPH <{graph_iri}> {{
    ?subject ?authorityPredicate ?authorityValue .
  }}
}}
WHERE {{
  GRAPH <{graph_iri}> {{
    VALUES ?subject {{ <{observation_iri}> }}
    ?subject ?authorityPredicate ?authorityValue .
    FILTER(?authorityPredicate IN ({
      ", ".join(prefix + ":" + predicate for predicate in predicates)
    }))
  }}
}};
INSERT DATA {{
  GRAPH <{graph_iri}> {{
    <{observation_iri}>
{inserts}
  }}
}}
""".strip()
    fuseki.update(dataset, sparql)
    # A failed Fuseki update never advances the receipt; retry is idempotent.
    await db.execute(
        """
        INSERT INTO aios.rdf_observation_authority_projection
            (rdf_dataset, rdf_graph, observation_iri, projection_hash, updated_at)
        VALUES ($1,$2,$3,$4,now())
        ON CONFLICT (rdf_dataset, rdf_graph, observation_iri)
        DO UPDATE SET projection_hash=EXCLUDED.projection_hash, updated_at=now()
        """,
        dataset, graph_iri, observation_iri, fingerprint,
    )


async def compact_character_world_epistemic_shadows(
    db: Database,
    fuseki: FusekiClient,
    *,
    limit: int = 100,
) -> int:
    """Remove legacy /world:epistemic shadows for character-owned observations.

    /char owns character-relative experience.  This repair is intentionally
    receipt-driven and bounded so old deployments can converge without a large
    Fuseki transaction.  Shared proposition resources are retained whenever any
    remaining world observation still references them.
    """
    rows = await db.fetch(
        """
        SELECT rpl.claim_id, o.observation_id, o.proposition_id
        FROM aios.rdf_promotion_log rpl
        JOIN aios.observation o ON o.claim_id=rpl.claim_id
        JOIN aios.claim_context_resolution ccr ON ccr.claim_id=rpl.claim_id
        WHERE rpl.rdf_dataset=$1
          AND rpl.rdf_graph=$2
          AND rpl.rdf_predicate=$3
          AND ccr.origin_character_id IS NOT NULL
        ORDER BY rpl.promoted_at, rpl.claim_id
        LIMIT $4
        """,
        DATASET, GRAPH_IRI, RECEIPT_PREDICATE, limit,
    )
    pending: list[tuple[object, str]] = []
    for row in rows:
        obs_iri = f"urn:aios:observation:{row['observation_id']}"
        prop_iri = f"urn:aios:proposition:{row['proposition_id']}"
        sparql = f"""
DELETE WHERE {{
  GRAPH <{GRAPH_IRI}> {{ <{obs_iri}> ?p ?o . }}
}};
DELETE {{
  GRAPH <{GRAPH_IRI}> {{ <{prop_iri}> ?p ?o . }}
}}
WHERE {{
  GRAPH <{GRAPH_IRI}> {{
    <{prop_iri}> ?p ?o .
    FILTER NOT EXISTS {{
      ?objective_observation world:observesProposition <{prop_iri}> .
      FILTER(?objective_observation != <{obs_iri}>)
    }}
  }}
}}
""".strip()
        pending.append((row["claim_id"], sparql))
        if len(pending) >= 20:
            # One bounded Fuseki transaction per batch. Never acknowledge a
            # receipt before its batch successfully reaches the derived graph.
            fuseki.update(
                DATASET,
                "PREFIX world: <urn:aios:world#>\n"
                + ";\n".join(statement for _, statement in pending),
            )
            for claim_id, _ in pending:
                await db.execute(
                    """
                    DELETE FROM aios.rdf_promotion_log
                    WHERE claim_id=$1 AND rdf_dataset=$2
                      AND rdf_graph=$3 AND rdf_predicate=$4
                    """,
                    claim_id, DATASET, GRAPH_IRI, RECEIPT_PREDICATE,
                )
            pending.clear()
    if pending:
        fuseki.update(
            DATASET,
            "PREFIX world: <urn:aios:world#>\n"
            + ";\n".join(statement for _, statement in pending),
        )
        for claim_id, _ in pending:
            await db.execute(
                """
                DELETE FROM aios.rdf_promotion_log
                WHERE claim_id=$1 AND rdf_dataset=$2
                  AND rdf_graph=$3 AND rdf_predicate=$4
                """,
                claim_id, DATASET, GRAPH_IRI, RECEIPT_PREDICATE,
            )
    return len(rows)


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
          AND aios.semantic_claim_topology_admitted(o.claim_id)
        """,
        claim_id,
    )
    if not row:
        return False

    perceiver_rows = await db.fetch(
        """
        SELECT DISTINCT kae.instance_id
        FROM aios.knowledge_acquisition_event kae
        WHERE kae.claim_id=$1
          AND kae.instance_id IS NOT NULL
        """,
        claim_id,
    )

    authority = await db.fetchrow(
        """
        SELECT
            kae.acquisition_id,
            eaa.origin_kind,
            eaa.epistemic_mode,
            eaa.authority_state,
            eaa.authority_rank,
            eaa.lineage_key,
            eaa.predicate_class,
            eaa.authorized_uses,
            eaa.policy_version
        FROM aios.knowledge_acquisition_event kae
        JOIN aios.epistemic_authority_admission eaa
          ON eaa.acquisition_id=kae.acquisition_id
        WHERE kae.claim_id=$1
        ORDER BY
            (kae.instance_id IS NOT DISTINCT FROM $2::uuid) DESC,
            kae.created_at,
            kae.acquisition_id
        LIMIT 1
        """,
        claim_id,
        row["character_instance_id"],
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

    obs_iri = f"urn:aios:observation:{row['observation_id']}"
    prop_iri = f"urn:aios:proposition:{row['proposition_id']}"
    claim_iri = f"urn:aios:world:claim:{claim_id}"
    char_owner_segment = quote(character_id, safe="") if character_id else None
    char_obs_iri = (
        f"urn:aios:char:{char_owner_segment}:observation:{row['observation_id']}"
        if char_owner_segment else None
    )

    # /char and /world are epistemic ownership boundaries, not duplicate indexes.
    # A character-owned acquisition is subjective/experienced knowledge and must
    # not acquire a shadow world:Observation merely because it was normalized.
    character_owned = bool(character_id)

    if not character_owned:
        # Authority is mutable control-plane state while origin/lineage are immutable.
        # Refresh it independently of the legacy projection receipt so objective
        # observations gain the authority membrane when they are next projected.
        await _project_observation_authority(
            db,
            fuseki,
            dataset=DATASET,
            graph_iri=GRAPH_IRI,
            observation_iri=obs_iri,
            prefix="world",
            namespace="urn:aios:world#",
            authority=authority,
        )
    if character_id and char_graph and char_obs_iri:
        await _project_observation_authority(
            db,
            fuseki,
            dataset="char",
            graph_iri=char_graph,
            observation_iri=char_obs_iri,
            prefix="char",
            namespace="urn:aios:char#",
            authority=authority,
        )

    if character_owned and world_receipt:
        # Repair legacy normalized-observation shadows.  Delete only this
        # character-owned observation and then remove its proposition resource
        # iff no remaining objective observation references it.
        cleanup = f"""
PREFIX world: <urn:aios:world#>
DELETE WHERE {{
  GRAPH <{GRAPH_IRI}> {{ <{obs_iri}> ?p ?o . }}
}};
DELETE {{
  GRAPH <{GRAPH_IRI}> {{ <{prop_iri}> ?p ?o . }}
}}
WHERE {{
  GRAPH <{GRAPH_IRI}> {{
    <{prop_iri}> ?p ?o .
    FILTER NOT EXISTS {{
      ?objective_observation world:observesProposition <{prop_iri}> .
      FILTER(?objective_observation != <{obs_iri}>)
    }}
  }}
}}
""".strip()
        fuseki.update(DATASET, cleanup)
        await db.execute(
            """
            DELETE FROM aios.rdf_promotion_log
            WHERE claim_id=$1
              AND rdf_dataset=$2
              AND rdf_graph=$3
              AND rdf_predicate=$4
            """,
            claim_id, DATASET, GRAPH_IRI, RECEIPT_PREDICATE,
        )
        world_receipt = None

    if (character_owned and char_receipt) or (not character_owned and world_receipt):
        # A retry may still have belief mutations waiting in the SQL outbox, but
        # if there are none project_character_belief_state performs zero RDF I/O.
        for perceiver in perceiver_rows:
            await project_character_belief_state(
                db,
                fuseki,
                instance_id=perceiver["instance_id"],
            )
        return True

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
{chr(10).join(_authority_insert_lines("world", authority))}
      prov:wasDerivedFrom <{claim_iri}> .
  }}
}}
""".strip()

    if not character_owned and not world_receipt:
        fuseki.update(DATASET, sparql)

    if character_id and not char_receipt:
        char_dataset = "char"
        char_graph = char_graph or f"urn:aios:char:{char_owner_segment}:epistemic"
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
PREFIX xsd:   <http://www.w3.org/2001/XMLSchema#>

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
{chr(10).join(_authority_insert_lines("char", authority))}
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

    if not character_owned and not world_receipt:
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
