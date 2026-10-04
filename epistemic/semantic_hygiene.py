"""RDF-assisted, source-grounded semantic hygiene (shadow mode only).

PostgreSQL remains authoritative. This module discovers suspicious semantic
identities, checks whether they are present in /char, and persists immutable
source-linked proposals and projected impact. It NEVER deletes or updates
claims, evidence, semantic atoms, character beliefs, topology or Fuseki.

First pass: the V3-only evidence population and a bounded suspicious V4 sample.
A cursor and (atom, policy, source revision signature) deduplication bound work.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any
from uuid import UUID

from aios_app.db import Database
from aios_app.rdf.fuseki import FusekiClient

logger = logging.getLogger("aios.epistemic.semantic_hygiene")
POLICY_VERSION = "semantic-hygiene-shadow-v1"
POPULATIONS = ("v3_only", "v4_sample")
MAX_BATCH = 16
MAX_SOURCE_ROWS = 128

_CANDIDATES = """
WITH support AS (
    SELECT DISTINCT p.atom_id, o.claim_id, si.validator_version
    FROM aios.proposition_evidence pe
    JOIN aios.proposition p ON p.proposition_id=pe.proposition_id
    JOIN aios.observation o ON o.observation_id=pe.observation_id
    JOIN aios.claim_semantic_integrity si ON si.claim_id=o.claim_id
    WHERE si.status='valid'
), coverage AS (
    SELECT atom_id,
      BOOL_OR(validator_version='semantic-integrity-v3-fidelity') AS has_v3,
      BOOL_OR(validator_version='semantic-integrity-v4-source-coverage') AS has_v4
    FROM support GROUP BY atom_id
)
SELECT a.atom_id, a.subject_norm, a.predicate_norm, a.object_norm
FROM coverage c
JOIN aios.semantic_atom a ON a.atom_id=c.atom_id
WHERE ($1::uuid IS NULL OR a.atom_id > $1)
  AND (
    ($2='v3_only' AND c.has_v3 AND NOT c.has_v4)
    OR
    ($2='v4_sample' AND c.has_v4
      AND EXISTS (SELECT 1 FROM aios.character_belief_state bs
                  WHERE bs.atom_id=a.atom_id)
      AND (
        COALESCE(a.subject_norm,'') ~* '(^and | and$|^but |, *,)'
        OR COALESCE(a.object_norm,'') ~* '( and| or)$'
        OR a.predicate_norm IN ('go','keep','pack')
        OR NULLIF(a.object_norm,'') IS NULL
      ))
  )
ORDER BY a.atom_id
LIMIT $3
"""

# Only materialized evidence can be withdrawn during a future, separately
# authorized reconciliation pass. No anti-join from semantic_atom alone is
# treated as proof of error.
_PROVENANCE = """
SELECT
    p.proposition_id, p.canonical_text,
    pe.evidence_id, pe.evidence_role,
    o.observation_id, cc.claim_id, cc.raw_text,
    si.revision_key, si.validator_version, si.status AS integrity_status,
    op.frame_id, op.is_primary, op.semantic_role,
    f.frame_index, f.subject_text, f.resolved_subject,
    f.predicate_surface, f.predicate_canonical,
    f.object_text, f.resolved_object, f.modality, f.meta AS frame_meta
FROM aios.proposition p
JOIN aios.proposition_evidence pe ON pe.proposition_id=p.proposition_id
JOIN aios.observation o ON o.observation_id=pe.observation_id
JOIN aios.claim_candidate cc ON cc.claim_id=o.claim_id
LEFT JOIN aios.claim_semantic_integrity si ON si.claim_id=cc.claim_id
LEFT JOIN aios.observation_proposition op
  ON op.observation_id=o.observation_id AND op.proposition_id=p.proposition_id
LEFT JOIN aios.claim_semantic_frame f ON f.frame_id=op.frame_id
WHERE p.atom_id=$1
ORDER BY cc.claim_id, p.proposition_id, op.frame_id, pe.evidence_id
LIMIT $2
"""

_IMPACT = """
SELECT
  (SELECT COUNT(*) FROM aios.proposition WHERE atom_id=$1) AS propositions,
  (SELECT COUNT(*) FROM aios.character_belief_state WHERE atom_id=$1)
    AS belief_states,
  (SELECT COUNT(DISTINCT instance_id) FROM aios.character_belief_state
   WHERE atom_id=$1) AS affected_instances,
  (SELECT COUNT(*) FROM aios.proposition_evidence pe
   JOIN aios.proposition p ON p.proposition_id=pe.proposition_id
   WHERE p.atom_id=$1) AS evidence_records,
  (SELECT COUNT(*) FROM aios.semantic_topology_node n
   JOIN aios.proposition p ON p.proposition_id=n.proposition_id
   WHERE p.atom_id=$1) AS topology_nodes,
  (SELECT COUNT(*) FROM aios.semantic_anchor_edge ae
   JOIN aios.proposition p ON p.proposition_id=ae.proposition_id
   WHERE p.atom_id=$1) AS topology_anchors
"""

def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)


def _source_entry(row: Any) -> dict[str, Any]:
    """Freeze only lineage and fields needed to reproduce the diagnosis."""
    meta = row["frame_meta"] if isinstance(row["frame_meta"], dict) else {}
    return {
        key: row[key] for key in (
            "proposition_id", "canonical_text", "evidence_id", "evidence_role",
            "observation_id", "claim_id", "raw_text", "revision_key",
            "validator_version", "integrity_status", "frame_id", "is_primary",
            "semantic_role", "frame_index", "subject_text", "resolved_subject",
            "predicate_surface", "predicate_canonical", "object_text",
            "resolved_object", "modality",
        )
    } | {"frame_meta": {
        key: meta.get(key) for key in (
            "source_subject_text", "source_object_text", "standalone_semantic",
            "clause_relation", "root_dep", "local_relative_antecedent",
            "interpretation_status",
        )
    }}


def classify_candidate(atom: Any, provenance: list[dict[str, Any]],
                       impact: Any, rdf_graphs: list[str],
                       *, truncated: bool = False) -> tuple[str, list[str]]:
    """Conservative signals for REVIEW, not an autonomous deletion decision."""
    subject = str(atom["subject_norm"] or "").strip().casefold()
    predicate = str(atom["predicate_norm"] or "").strip().casefold()
    obj = str(atom["object_norm"] or "").strip().casefold()
    reasons: set[str] = set()

    if not provenance:
        reasons.add("no_current_materialized_evidence")
    if truncated:
        reasons.add("source_lineage_truncated")
    if not subject or not predicate:
        reasons.add("missing_core_semantic_component")
    if re.search(r"(^and\b|\band$|^but\b|,\s*,)", subject):
        reasons.add("suspect_subject_boundary")
    if re.search(r"\b(and|or)$", obj):
        reasons.add("truncated_coordinated_object")
    if not obj and predicate in {"have", "give", "put", "tell", "prefer", "keep"}:
        reasons.add("missing_expected_argument")

    for source in provenance:
        meta = source["frame_meta"]
        if meta.get("standalone_semantic") is False and meta.get(
            "clause_relation"
        ) in {"relcl", "acl", "advcl", "xcomp", "conj"}:
            reasons.add("dependent_frame_requires_parent")
        raw = str(source["raw_text"] or "").casefold()
        if predicate == "go" and re.search(
            r"\b(?:gone|went|goes|going)\s+still\b", raw
        ):
            reasons.add("resultative_state_lost")

    if int(impact["belief_states"] or 0) and not rdf_graphs:
        # A lagging /char projector can also cause this; never interpret as
        # evidence of a bad proposition by itself.
        reasons.add("rdf_not_observed_for_belief")

    if truncated or "no_current_materialized_evidence" in reasons:
        disposition = "needs_review"
    elif reasons & {
        "missing_core_semantic_component", "suspect_subject_boundary",
        "truncated_coordinated_object", "missing_expected_argument",
        "resultative_state_lost",
    }:
        disposition = "repair_candidate"
    elif "dependent_frame_requires_parent" in reasons:
        disposition = "demote_candidate"
    elif reasons:
        disposition = "needs_review"
    else:
        disposition = "retain"
    # A retraction requires source-based adjudication AND an independent
    # authority path; the shadow classifier intentionally never authorizes it.
    return disposition, sorted(reasons)


def _rdf_presence(fuseki: FusekiClient, atoms: list[Any]) -> dict[str, list[str]]:
    """One bounded read-only SPARQL query, restricted to DB-generated UUIDs."""
    if not atoms:
        return {}
    iris = " ".join(
        f"<urn:aios:semantic-atom:{UUID(str(row['atom_id']))}>"
        for row in atoms
    )
    sparql = (
        "SELECT DISTINCT ?atom ?graph WHERE { VALUES ?atom { " + iris +
        " } GRAPH ?graph { ?atom a <urn:aios:char#SemanticAtom> . } }"
    )
    result = fuseki.query("char", sparql)
    found: dict[str, list[str]] = {}
    for binding in result.get("results", {}).get("bindings", []):
        atom_iri = binding.get("atom", {}).get("value", "")
        graph = binding.get("graph", {}).get("value", "")
        prefix = "urn:aios:semantic-atom:"
        if atom_iri.startswith(prefix) and graph:
            found.setdefault(atom_iri[len(prefix):], []).append(graph)
    return {key: sorted(set(values)) for key, values in found.items()}


async def run_shadow_batch(db: Database, fuseki: FusekiClient, *,
                           population: str = "v3_only",
                           limit: int = MAX_BATCH) -> dict[str, Any]:
    """One bounded sweep; records proposals only. A failure never moves cursor."""
    if population not in POPULATIONS:
        raise ValueError(f"unknown hygiene population: {population}")
    limit = max(1, min(MAX_BATCH, int(limit)))
    state = await db.fetchrow(
        """SELECT last_atom_id, completed_at
           FROM aios.semantic_hygiene_shadow_cursor
           WHERE population=$1 AND policy_version=$2""",
        population, POLICY_VERSION,
    )
    if state is None:
        raise RuntimeError("semantic hygiene migration not installed")
    if state["completed_at"] is not None:
        return {"population": population, "scanned": 0, "complete": True}

    atoms = await db.fetch(_CANDIDATES, state["last_atom_id"], population, limit)
    if not atoms:
        await db.execute(
            """UPDATE aios.semantic_hygiene_shadow_cursor
               SET completed_at=now(), updated_at=now()
               WHERE population=$1 AND policy_version=$2""",
            population, POLICY_VERSION,
        )
        return {"population": population, "scanned": 0, "complete": True}

    # Do not advance the cursor on Fuseki errors: incomplete RDF evidence must
    # never masquerade as a negative query result.
    presence = _rdf_presence(fuseki, atoms)
    recorded = 0
    for atom in atoms:
        atom_id = atom["atom_id"]
        rows = await db.fetch(_PROVENANCE, atom_id, MAX_SOURCE_ROWS + 1)
        truncated = len(rows) > MAX_SOURCE_ROWS
        lineage = [_source_entry(row) for row in rows[:MAX_SOURCE_ROWS]]
        impact = await db.fetchrow(_IMPACT, atom_id)
        impact_data = dict(impact or {})
        graphs = presence.get(str(atom_id), [])
        disposition, reasons = classify_candidate(
            atom, lineage, impact_data, graphs, truncated=truncated,
        )
        claim_ids = sorted({
            str(row["claim_id"]) for row in lineage if row["claim_id"]
        })
        revision_keys = sorted({
            str(row["revision_key"]) for row in lineage if row["revision_key"]
        })
        source_signature = hashlib.sha256(_json(
            [atom["subject_norm"], atom["predicate_norm"], atom["object_norm"],
             lineage, impact_data]
        ).encode("utf-8")).hexdigest()

        result = await db.execute_returning_row(
            """INSERT INTO aios.semantic_hygiene_shadow_audit
               (atom_id, population, policy_version, source_signature,
                disposition, reason_codes, source_claim_ids, source_revision_keys,
                rdf_graphs, impact, evidence_snapshot)
               VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb,$8::jsonb,
                       $9::jsonb,$10::jsonb,$11::jsonb)
               ON CONFLICT (atom_id, policy_version, source_signature)
               DO NOTHING RETURNING audit_id""",
            atom_id, population, POLICY_VERSION, source_signature,
            disposition, _json(reasons), _json(claim_ids), _json(revision_keys),
            _json(graphs), _json(impact_data),
            _json({"atom": {
                "subject": atom["subject_norm"], "predicate": atom["predicate_norm"],
                "object": atom["object_norm"],
            }, "sources": lineage, "truncated": truncated}),
        )
        recorded += int(result is not None)

    await db.execute(
        """UPDATE aios.semantic_hygiene_shadow_cursor
           SET last_atom_id=$3, updated_at=now()
           WHERE population=$1 AND policy_version=$2
             AND (last_atom_id IS NOT DISTINCT FROM $4::uuid)
             AND completed_at IS NULL""",
        population, POLICY_VERSION, atoms[-1]["atom_id"], state["last_atom_id"],
    )
    logger.info(
        "Hygiene shadow population=%s scanned=%d new_audits=%d cursor=%s",
        population, len(atoms), recorded, atoms[-1]["atom_id"],
    )
    return {"population": population, "scanned": len(atoms),
            "new_audits": recorded, "complete": False}
