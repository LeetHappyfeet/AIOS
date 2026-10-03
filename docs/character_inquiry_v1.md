# Character Inquiry v1 — shared evidence lookup, separate authorities

Character Inquiry is a post-ingestion, instance-bound, read-only information-need service.
It is not another semantic interpreter, corpus acquisition process, or goal-admission
authority. This patch adds a deterministic source-ancestry adapter, scoped /char,
world and corpus lookups, a micro query-only planner, revision-relative audit, and
post-enrichment V11 shadow diagnostics.

## Integration

- V11 parser and goal source admission remain unchanged. Completed enrichment
  diagnostics with exact reason codes for unresolved reference/attribution are
  enqueued into a BACKGROUND shadow job. The shadow job only runs source-local
  DAG ancestry; its results cannot amend a goal.
- The same InquiryDemand/InquiryEvidence contract is available to downstream
  character cognition using evidence_scope=character_accessible.
- Cognitive knowledge-gap opportunities now select inquiry.resolve, which uses
  deterministic retrieval first. Only explicitly opted-in difficult demands
  may queue a separate character_inquiry_inference job.
- Inference uses the existing broker with worker_class=research and HUD profile
  inquiry_query. It emits only one question, query and target. The host validates
  and executes the query. One planning call per evidence fingerprint; no nested
  inquiry or implicit retry of failed inference.
- Source-local repair cannot consult /char, /world or corpus. Queries in
  character-accessible mode reuse existing retrieval ACLs and lineage scope.
- Every hit retains source identity and durable_knowledge=false for references.
  An inquiry's resolved/partial status is retrieval support, not admissibility.
- Query output cannot create goals, adjust semantic integrity, or enter
  corpus learning. Source Comparison remains an independent audit authority.

## Inspecting shadow outcomes

SELECT origin,uncertainty_kind,evidence_scope,status,model_calls,
       result->>'reason' AS reason,created_at
FROM aios.character_inquiry
WHERE instance_id = '<instance UUID>'::uuid
ORDER BY created_at DESC LIMIT 30;

## Initial limits

Up to three evidence hits, ancestry depth three, one model pass, no nested
inquiry, micro planner input capped at 1800 characters under an inquiry-specific
token profile, and maximum 150 generated model tokens. Character opportunity
model escalation is opt-in; V11 source-shadow lookups never invoke a model.
