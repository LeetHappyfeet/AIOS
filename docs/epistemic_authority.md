# Epistemic authority membrane

AIOS keeps objective `/world` state and subjective `/char` cognition separate. The authority membrane makes transition rules explicit without flattening creative character cognition.

`semantic_evidence_admission` answers whether parsed evidence is semantically usable. `epistemic_authority_admission` separately records what that acquisition may establish. Confidence is not authority.

Each acquisition receives an immutable `origin_kind` and `lineage_key`, plus an `epistemic_mode`, authority state/rank, and authorized uses. Generated/model cognition defaults to candidate authority and remains usable for belief and reflection, but does not become historical authority merely because it was persisted, retrieved, summarized, or repeated. External testimony/corpus evidence is attested; typed deterministic machine evidence may satisfy action preconditions.

Callers that know provenance should pass `origin_kind`, `epistemic_mode`, and `origin_lineage_id` to `record_acquisition()`. Otherwise the SQL classifier derives a conservative classification from acquisition/source metadata. Explicit lineage IDs are preferred for summaries, reflections, and other descendants of generated cognition so they cannot masquerade as independent corroboration.

The invariant is: persistence != authority; confidence != authority; origin is immutable; descendants retain their evidence family; generated cognition may enrich `/char` without authoring `/world`.

World mutation remains the responsibility of the causal integrity kernel.
