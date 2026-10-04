# Source-grounded managed goals and bounded inquiry

Revision: goal-source-admission-v1, formation source-selection v1; October 2026.

This patch makes planning.form_goal select only an independently admitted,
positive, owner-authored V11 source GOAL. The model proposal is a selector,
not the persisted objective. Selection requires an identical substantive
source-objective term set, uniquely identifying a single admitted source unit.
An unowned, figurative, negated, hypothetical or unsupported goal is rejected.
The original source objective, topic and provenance pass through
CharacterGoalService.reconcile_evidence, never directly into create().

Incremental migration 20261004_19_source_goal_admission.sql adds:
- character_goal_admission_receipt: idempotent source/objective/policy eligibility,
  independent of Integrity V4 and topology. Eligibility does not equal goal
  creation, goal completion, or factual truth.
- character_inquiry.goal_id: optional persistent character-scoped provenance.
  Source-local V11 repair CANNOT carry a goal_id or promote an intention.
- character_goal_research_link: many-to-many goal/dossier provenance; start()
  checks goal instance ownership and active status within its transaction.

Goal knowledge demand treats topology as candidate navigation, not proof:
unavailable, empty, or stale-only results do not suppress inquiry. It reports
topology_candidate_coverage separately from internal_coverage, the latter
derived from actual retrieved memory. Opportunity generation first issues
inquiry.resolve for a goal-specific knowledge question. If the prior goal
inquiry is unresolved, it proposes research.advance on a bounded dossier.
The existing progressive.available check suppresses exhausted research.
Research references do not create claims or mark the goal completed. Study
remains a distinct explicit ACL/revision-checked operation.

Recovery of V11 rejected pronoun/attribution candidates remains a diagnostic
source-local shadow operation. A partial ancestry hit is NOT evidence of a
uniquely resolved referent and must not be auto-promoted. This is deliberately
conservative until a formally verified referent-binding review is added.

Deployment: run `python -m aios_app.migrate` before restarting API, runner and
cognitive workers. Rollout is additive; do not replay or mutate terminal goals.
Suggested regression run:

```bash
pytest -q tests/test_source_goal_formation_receipt.py \
  tests/test_cognition_v11_source_admission.py \
  tests/test_goal_knowledge_demand.py \
  tests/test_character_inquiry.py \
  tests/test_progressive_research.py \
  tests/test_agent_goals.py
```

Read-only inspection:

```sql
SELECT source_node_id, source_unit_id, objective, decision, reason, created_at
FROM aios.character_goal_admission_receipt
WHERE instance_id = '<INSTANCE_UUID>'::uuid ORDER BY created_at DESC LIMIT 20;

SELECT goal_id, origin, status, result, updated_at FROM aios.character_inquiry
WHERE instance_id = '<INSTANCE_UUID>'::uuid AND goal_id IS NOT NULL
ORDER BY updated_at DESC LIMIT 20;

SELECT l.goal_id, l.dossier_id, d.question, d.status, d.cycles_completed
FROM aios.character_goal_research_link l
JOIN aios.character_research_dossier d ON d.dossier_id=l.dossier_id
WHERE l.instance_id='<INSTANCE_UUID>'::uuid;
```


## Source-repair authority and bounded retries

Character Inquiry annotates source-local evidence with
`goal_admission_review.decision=deferred`. Even a verified source anchor
is not a unique referent binding. No shadow inquiry may write a managed goal.
Goal research moves from inquiry.resolve to research.advance only after an
unresolved goal-scoped inquiry; planning/partial/resolved/conflicting receipts
and no_access do not cause an automatic research loop.

A goal-origin dossier refuses new automatic research cycles once all linked
owned goals are non-active. Idempotent replays of prior steps remain inspectable;
independent manual dossiers are not implicitly closed by goal lifecycle.
