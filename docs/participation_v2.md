# Participation context audit and paired shadow comparison

This patch keeps production memory and semantic admission unchanged. The original
`participation-shadow-v1` result remains the primary ledger population. Every new
worker evaluation also stores `signals.comparison` for `participation-shadow-v2`,
using the same claim, context snapshot, and conflict evidence.

## Context audit findings

| Input | Writer and scope | Evaluator behavior |
| --- | --- | --- |
| Goals | `epistemic/goals.py`, exact runtime instance; persistent intentions are explicitly carried by the goal service | Active goals only. Scheduled, dormant and terminal goals are sampled in coverage but do not become current attention goals. The evaluator does not call lifecycle writers. |
| Identity facets | Card bootstrap stages description as appearance, personality as personality, and dialogue examples as expression. `accept_identity_candidate` promotes candidates into character-wide durable facets. Manual edits also stage candidates before acceptance. | Accepted active personality, values, interests, preferences, roles and constraints participate. Appearance, expression examples and domain membership do not imply interest. Staged candidates are audit evidence only. |
| Relationships | `character_relationship`, exact observer instance, joined to `world_entity` | No production writer was found in this branch. An empty table is missing structured relationship evidence, not proof that the character has no relationships. Missing entity joins are reported. Domain affinity is a separate concept. |

All samples are bounded at 64 rows with truncation flags. Empty context cannot by
itself identify a database mismatch or prove absence throughout the character's
history. No sibling instances are searched or imported. Context is captured at
**evaluation time**, not reconstructed as of the original observation time.

## V2 decisions

- Separate authorship, source routing target and explicit subject/object participation.
  Addressed speech and scene presence remain unknown without claim-level evidence;
  a message target is not treated as proof of being addressed.
- Participation alone does not foreground a claim.
- Conservative representation checks flag missing subjects/predicates/arguments and
  generic subjects. An explicit intransitive predicate may omit its object. This is
  a heuristic requiring review, not a truth or extraction validator.
- Foreground requires usable representation plus goal/facet/conflict evidence,
  relationship relevance with two occurrences, or explicit participation with three.
- Independent recurrence counts distinct source DAG nodes, falling back to whole
  document IDs; multiple claims within one source do not count separately. This is
  conservative source independence, not proof of distinct real-world events or
  semantic equivalence. Missing source coordinates contribute no occurrences.
- Missing relevance context stays unknown. Sources and memories are retained.

## Run after pulling

Restart AIOS to load the API code and restart the separate participation worker:

```bash
python -m aios_app.agent.participation
```

The existing experiment can continue if its admission window is still open. No
migration or new experiment is required. New evaluations automatically include
both policies. Already queued claims use the updated worker.

Inspect live context in the browser:

```text
http://192.168.1.217:8000/agent/instance/5c4c475e-08d2-49f2-b25c-12ee005a3b72/participation/context
```

Replay one batch of up to 100 existing evaluations (default 50):

```bash
curl --fail-with-body -sS -X POST \
 'http://192.168.1.217:8000/agent/instance/5c4c475e-08d2-49f2-b25c-12ee005a3b72/participation/experiments/e696c395-9dad-4316-9aa6-197b3d66e1e1/compare?limit=100'
```

Repeat until `compared` is zero. This writes only comparison annotations, preserving
v1 decisions and frozen snapshots. Old snapshots omitted source involvement,
predicate and occurrence coordinates. Replay explicitly marks those unknown and
cannot reconstruct them; compare `frozen_legacy_replay` and `paired_live` separately.

The existing GET experiment report includes a `comparisons` cross-tab grouped by
v1 population, v2 population and evaluation mode. Detailed results are under
`evaluations[].signals.comparison`. `limit=200` exports up to 200 detail rows.

Review policy disagreements on 5–6 new chat pairs, plus significant claims that
both policies leave latent. Label roughly 50 examples foreground/latent/background
and record a short reason. Measure precision, missed significant claims, context
coverage, source recurrence, and evaluation latency. A smaller foreground share
alone is not success. The evaluator is downstream of character knowledge; it does
not yet demonstrate reduced embedding cost or faster upstream ingestion.
