# Inspect an AIOS interaction

Start with the `session_id`, `instance_id`, `node_id`, and `event_id` printed by
[the example client](../examples/live_client.py). Preserve these in your client
logs so a missing memory can be traced to its source.

## Start at the HTTP boundary

Set `INSTANCE` and `NODE` to the actual returned UUIDs:

```bash
BASE=http://127.0.0.1:8000
INSTANCE=REPLACE_WITH_INSTANCE_UUID
NODE=REPLACE_WITH_NODE_UUID
curl --fail-with-body -sS "$BASE/instance/$INSTANCE/state"
curl --fail-with-body -sS -X POST "$BASE/instance/$INSTANCE/hud?through_node_id=$NODE&wait_ms=2500"
curl --fail-with-body -sS "$BASE/agent/runtime/versions"
```

Instance state exposes runtime/source coordinates. HUD `freshness` explains the
requested coordinate and readiness. Do not compare the runtime `head_node_id`
with a source `node_id` as if they were necessarily the same timeline.

The version manifest separates installed code, effective database authority,
shadow comparators, migration receipts, SQL fingerprints, and operation receipt
versions. An installed V4 module is not evidence that V4 governs live admission.
See [the release-gate explanation](experiment8_pre_reset_release_gate.md).

## Read-only SQL investigation

For default Compose credentials, enter psql from the repository root:

```bash
docker compose exec postgres psql -U postgres -d postgres
```

For custom credentials, substitute the actual user and database. Run the
following in **psql**, replacing the UUID values. `\set` and `:'name'` are psql
syntax, not generic SQL; in another SQL client use its parameter mechanism.

```sql
\set iid 'REPLACE_WITH_INSTANCE_UUID'
\set nid 'REPLACE_WITH_NODE_UUID'
BEGIN READ ONLY;
SET LOCAL statement_timeout = '5s';

SELECT node_id, timeline_id, event_id, speaker_id, speaker_role,
       viewpoint_id, kind, message_text
FROM aios.dag_node
WHERE node_id = :'nid'::uuid;

SELECT instance_id, state_version, timeline_id, head_node_id,
       source_timeline_id, source_head_node_id, updated_at
FROM aios.character_runtime_state
WHERE instance_id = :'iid'::uuid;

SELECT ds.section_id, ds.claims_extracted_at, es.sentence_id,
       es.sentence_text, cc.claim_id, cc.subject, cc.predicate,
       cc.object, cc.status, cc.pruning_reason
FROM aios.document_section ds
LEFT JOIN aios.extracted_sentence es ON es.section_id = ds.section_id
LEFT JOIN aios.claim_candidate cc ON cc.sentence_id = es.sentence_id
WHERE ds.node_id = :'nid'::uuid
ORDER BY ds.section_order, es.sentence_index, cc.claim_id
LIMIT 100;

SELECT job_type, status, resource_class, count(*) AS jobs,
       min(run_after) AS earliest_run_after
FROM aios.pipeline_job
WHERE status IN ('queued', 'running', 'failed')
GROUP BY job_type, status, resource_class
ORDER BY job_type, status;

SELECT job_id, job_type, status, resource_class, partition_key,
       attempts, run_after, heartbeat_at, last_error
FROM aios.pipeline_job
WHERE last_error IS NOT NULL
ORDER BY updated_at DESC
LIMIT 20;
COMMIT;
```

The first query proves which message was stored. The second locates the active
instance's source cursor. The third shows extraction coverage: absent sections
suggest projection has not reached this source, null sentences/claims suggest
later extraction is pending or produced no claims. Rows and status labels alone
do not establish semantic validity or accepted belief. Queue queries are global;
they identify stalled stages, not necessarily jobs belonging to this one message.
If a query times out, `ROLLBACK;` before continuing.

## Inspect semantic results and stores

Use `GET /epistemic/proposition/{proposition_id}` when you have a proposition
UUID. To inspect vector neighborhoods without guessing their meaning:

```bash
curl --fail-with-body -sS "$BASE/semantic/clusters?limit=20&offset=0"
```

Submit a returned cluster UUID to `POST /semantic/inspect` as
`{"cluster_id":"ACTUAL_UUID","limit":20,"evidence_limit":3}`. Its schema also
accepts a collection and point UUIDs. This diagnostic resolves groups to
propositions and source evidence. Cluster membership is not a truth verdict.

For direct infrastructure checks:

```bash
curl --fail-with-body -sS http://127.0.0.1:6333/collections
curl --fail-with-body -sS -G --data-urlencode 'query=ASK {}' \
  http://127.0.0.1:3030/world/sparql
curl --fail-with-body -sS -G --data-urlencode 'query=ASK {}' \
  http://127.0.0.1:3030/char/sparql
```

`ASK {}` verifies a SPARQL endpoint responds; it does not verify expected
knowledge exists. Consult [the ontology guide](../rdf/ontology/readme.md) for
graph meanings. PostgreSQL preserves source/provenance and operational state,
Fuseki represents semantics, and Qdrant supports candidate discovery. A vector
point or RDF triple alone does not prove a character acquired a valid belief.

## Diagnose by symptom

| Symptom | First checks |
|---|---|
| Activation returns 404 | Bootstrap the identity and check the exact `character_id` |
| Participant binding failure | Verify both identities/instances and matching session, user, scope, and actor IDs |
| Ingest succeeds, HUD is not ready | Source node and cursor, then pending/failed pipeline stages |
| HUD returns 409 | Compare requested node to current source head; check client turn ordering |
| Memory absent | Follow source extraction, semantic/integrity admission, character acquisition, then HUD selection |
| Belief unresolved | Inspect evidence and active family/authority policy; do not infer a defect from confidence alone |
| Goal absent | Check source attribution, cognition/admission, lifecycle, and HUD selection; not every intention becomes a goal |
| High CPU with slow progress | Inspect job resource classes, deferred `run_after`, failures, and worker settings |
| Experiment has no comparisons | Check enrollment, queue/worker state, and comparison receipts separately from installed versions |

Detailed subsystem guides include [belief reconciliation](belief_reconciliation.md),
[semantic integrity deployment](semantic_integrity_deployment.md),
[character inquiry](character_inquiry_v1.md), and
[temporal goal reviews](temporal_goal_reviews.md). These are deeper diagnostics;
the core client should continue to use the HTTP boundary.
