# Character belief reconciliation

AIOS keeps two different structures for character cognition:

- **Evidence topology** records what a character encountered, where it came from, and when it entered the DAG. Observations, `knowledge_acquisition_event`, `character_proposition_knowledge`, acquisition topology, source anchors, and conflicts remain durable provenance.
- **Belief topology** records what the character currently has reason to believe after admitted evidence is reconciled. It is materialized in `character_belief_state` and projected into `/char` as `BELIEF_STATE` topology nodes.

The invariant is **history expands; state converges**. Reconciliation never deletes contradictory or ambiguous evidence.

## Semantic atoms

`semantic_atom` is the polarity-independent identity of a semantic question:

```text
(subject_norm, predicate_norm, object_norm)
```

Positive and negative propositions therefore compete over the same atom. Propositions with no resolved semantic components remain distinct by canonical raw text so unresolved fragments are not collapsed into one atom.

## Evidence admission

`semantic_evidence_admission` separates semantic parsing from cognitive admission. Every character acquisition is classified as:

- `active`: sufficiently resolved evidence may affect current belief state.
- `unresolved`: preserved evidence is too ambiguous to use yet.
- `suppressed`: preserved evidence is not semantically usable as a proposition.

The first policy deliberately quarantines bare quoted discourse that is still classified as `narrated_observation`. Until a local quote speaker/addressee is resolved, transport-level chat identities must not be treated as local quoted participants.

## Belief reconciliation

For each `(instance_id, atom_id)`, the resolver:

1. walks only the instance and its ancestors;
2. excludes sibling experiential branches;
3. ignores superseded ingest evidence;
4. consumes only `active` admissions;
5. collapses correlated evidence from the same source/event coordinate;
6. aggregates positive and negative support with bounded independent-evidence accumulation;
7. materializes `positive`, `negative`, or `unresolved` stance.

Default policy:

```text
accept_support = 0.60
decision_margin = 0.15
```

A stance is accepted only when its support reaches the threshold and exceeds the opposite side by the required margin. Strong conflicting evidence therefore remains unresolved rather than being hidden by recency or a single confidence score.

## HUD boundary

`character_proposition_knowledge` remains evidence ownership. It is not current belief state.

`character_active_proposition_knowledge` is the compatibility projection for ordinary HUD retrieval. It exposes the representative proposition selected by `character_belief_state`, with belief confidence and stance, while retaining the instance that supplied the underlying evidence.

Raw evidence remains available for diagnostics and historical/explanation tooling.

## Automatic refresh

Database triggers refresh admission and belief state when:

- character proposition evidence changes;
- an admission decision changes;
- semantic frame/context resolution changes;
- an ingest event is superseded or restored.

Descendant character instances are refreshed when ancestor evidence changes, preserving branch inheritance without sibling leakage.

## Manual inspection

`aios_app.epistemic.belief_reconciliation` exposes helpers to force reconciliation or inspect materialized belief state from application code. The SQL materializer remains authoritative so ordinary ingestion does not depend on an LLM or vector classifier to decide current belief.
