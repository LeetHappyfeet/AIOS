# Semantic topology eligibility

Apply `migrations/current/20260930_01_semantic_topology_eligibility.sql` before running this version. Missing interpretation data fails closed; legacy claims need frame interpretation/backfill before participating again.

SQL observations, claims, propositions, frames and provenance remain intact. A proposition may enter the proposition/epistemic vector indexes only when an observation-to-proposition frame link has a resolved frame and a standalone semantic interpretation. Missing objects are not automatically disqualifying. This policy is semantic completeness, not a truth or modality decision.

Indexing precedes neighbor admission. Index eligibility therefore differs from topology admission: neighbor generation, relation classification, clustering, reconciliation and direct claim projection also require a supported occurrence with completed exact/neighbor admission. Timeout, unavailable and error bypasses preserve evidence but defer topology; the normal admission pass retries them. The inspector exposes `topology_eligible` and excludes ineligible points from representative/medoid selection.

A resolved occurrence can make a proposition eligible without certifying partial occurrences. Neighbor classification and event evidence selection use eligible occurrences. Eligibility is evaluated from current evidence, so repair can reopen participation without deleting observations or inventing a new proposition.

The semantic worker quarantines existing ineligible vectors in bounded batches, including ownership copies, and only removes index receipts after Qdrant confirms deletion. It marks affected current clusters stale, supersedes unsupported semantic-event memberships and removes unsupported derived topology nodes. Historical neighbor decisions, cluster snapshots and source evidence remain for audit. Topology deletions enter the existing RDF deletion outbox; Fuseki convergence follows the normal projection worker. Cleanup can require multiple passes.

Read-only audit:

```sql
SELECT * FROM aios.semantic_topology_eligibility_audit
WHERE NOT topology_eligible;
```

The eligibility helpers deliberately do not infer pronoun referents, repair malformed propositions or prove occurrence identity. Those remain separate semantic-resolution responsibilities.
