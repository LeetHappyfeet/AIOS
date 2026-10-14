# Goal Integration V1 and Goal Research V1

Additive to the canonical migration chain. No semantic admission rewrite, graph
reset, database wipe, or change in the authority of managed goals is required.

## Authority and data flow

* PostgreSQL `character_agent_goal` remains the only managed intention authority.
  Historical semantic GOAL claims/observations can explain how an intention
  arose, but RDF or Qdrant retrieval never reactivates or completes an old goal.
* Message Cognition's source-bound admission remains responsible for newly
  expressed self-authored intentions.
* `GoalKnowledgeDemandResolver` derives a **routing estimate** from independent,
  established, character-owned proposition IDs returned in the current cognitive
  snapshot. It excludes GOAL propositions, provisional cognition and candidate
  authority. Copies of one proposition in several RDF roles count only once.
  "Sufficient" requires three distinct propositions with broad term coverage
  and at least two source lineages. This is not proof the goal itself is done.
* SQL topology is optional navigation. A timeout marks requirement
  `unavailable` and produces no new research operation. Fallback memory is
  preserved, and the next retrieval cycle may try again.
* A managed goal is projected as one `goal_knowledge` cognitive subject and
  now has a durable, primary `character_goal_knowledge_requirement`. Each
  requirement retains question/query, coverage status and score, source/receipt
  IDs, retrieval status, and originating DAG coordinate.
* An unassessed requirement starts with one bounded character-relative inquiry.
  If a completed inquiry is partial, unresolved or otherwise does not establish
  sufficient support, a persistent `research.advance` is offered instead of
  looping `inquiry.resolve`. Repeated `no_access` is suppressed until a new
  corpus-index or scoped ACL/domain grant epoch appears.
* Stage 3 dossiers are attached to a goal AND the requirement via
  `character_goal_research_requirement_link`. Shared dossiers can support
  several requirements/goals with separate provenance. A dossier is still
  owned by an instance and its start independently validates active goal and
  requirement ownership. All study paths recheck current source access.
* Exhausted/no-question dossiers do not retry each cycle. If an additional
  indexed corpus section or a character corpus/domain grant appears after the
  last completed research step, one new queued research question may be
  inserted, subject to the dossier's existing cycle/section limits.
* Retrieval, dossier opening, searching, and source exposure are **not**
  source admission, belief authority, character experience, goal progress,
  or goal completion. A deliberate `research.study` still submits a passage
  through the existing ingestion and integrity boundary; a later knowledge
  coverage refresh must observe admitted, independent proposition receipts.
  Goal completion remains source-bound review/outcome reconciliation.

## Deployment

1. Pull `AIOS-development`.
2. Run `python -m aios_app.migrate` (or the launch preflight).
3. Apply immutable migration `20261004_20_goal_knowledge_dependencies.sql`
   after `20261004_19_source_goal_admission.sql`. Do not edit applied SQL.
4. Restart supervisor, runner, semantic index/topology, and API to pick up the
   new runtime code. There is no dependency on adding external corpus content.

## Observation queries

For an active goal instance, inspect coverage and persisted dossier linkage:

```sql
SELECT g.goal_id,g.goal_text,g.status,
       r.requirement_id,r.requirement_key,r.coverage_status,r.coverage_score,
       r.retrieval_state,r.evidence,r.updated_at
FROM aios.character_agent_goal g
LEFT JOIN aios.character_goal_knowledge_requirement r
  ON r.goal_id=g.goal_id AND r.instance_id=g.instance_id
WHERE g.instance_id='<INSTANCE_UUID>'::uuid
ORDER BY g.priority,r.updated_at DESC;

SELECT l.goal_id,l.requirement_id,l.dossier_id,d.status,d.cycles_completed,
       d.max_cycles,d.question
FROM aios.character_goal_research_requirement_link l
JOIN aios.character_research_dossier d ON d.dossier_id=l.dossier_id
WHERE l.instance_id='<INSTANCE_UUID>'::uuid;
```

With no reference corpus, one bounded unsuccessful research step is a correct
outcome. The parent's objective stays active; adding new permitted references
or granting an already-indexed scope subsequently allows one renewed dossier
question. The HUD shows compact requirement coverage and linked dossier count,
not invented research findings or false goal progress.

## Regression gates

`tests/test_goal_knowledge_dependencies.py` and updated
`tests/test_goal_knowledge_demand.py` and `tests/test_character_inquiry.py`
cover independent evidence, candidate-only topology, unavailable SQL, bounded
escalation, and no-access backoff. `tests/ci_progressive_research.py`
exercises real PostgreSQL requirement upsert, many-to-many dossier link,
idempotent replay and cross-instance authorization.
