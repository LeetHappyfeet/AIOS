# Knowledge Atlas Stabilization V1 (2026-10-04)

This is an additive patch to Stage 1–3. Do not wipe existing PostgreSQL,
Qdrant, Fuseki or character instances. The current cold reference corpus may
legitimately be empty; the patch does not create reference documents.

## Deployment order

1. Pull AIOS AIOS-development and run \`python -m aios_app.migrate\`.
   This installs \`20261004_18_knowledge_atlas_stabilization.sql\`, including
   the source-node keyed research action ledger and one-time advisory source
   receipt replay. The replay leaves underlying claims/frames/observations
   untouched and is consumed in bounded \`topic-discovery\` cycles.
2. Restart the AIOS services, including semantic index and topology workers.
3. Pull \`LeetHappyfeet/extension-MemoryVaultIngest\` on its \`main\` branch
   separately and refresh SillyTavern; the client-side adapter is NOT part of
   the AIOS repository. This version dispatches a single well-formed character
   research tool request after a live rendered message has been ingested.
   Historical transcript reconciliation never launches old requests.
4. Watch \`GET /agent/knowledge-atlas/health\` and confirm active opaque topics
   approach zero, pending retired vector deletions reach zero, and projection
   backlog converges once historical source discovery settles. Retired vectors
   are deleted only after Qdrant acknowledges; retired named graphs are cleared
   only through the dedicated topology worker with subsequent SQL receipts.

## Correctness boundaries

* An opaque proposition/topic key is source provenance, not a topic label.
  \`normalize_label\` rejects pure 32–64-character hexadecimal strings,
  UUID-shaped labels and obvious dangling function words. Readable semantic
  frame labels remain usable even where frame entity keys are opaque.
* Previously collected bad candidates are retired incrementally; their
  mentions and original input material are not manually deleted. A graph
  revision changes for genuinely changed mention membership or navigation
  edges, not merely a source revision receipt refresh.
* Scene resolution no longer treats the latest entire dialogue turn as a
  physical last change. Legacy snapshots with source=dag_node are hidden at
  presentation time, and character tool tags are stripped from rendered recent
  dialogue only. Full raw source remains available as ingested DAG evidence.
* A host-generated research call is routed by the client to
  \`POST /agent/instance/{instance_id}/research/tool-request\` with
  \`{"source_node_id":"<current-character-source-node-UUID>"}\`.
  The backend checks the current source timeline and the ingest event's
  \`viewpoint_id\` against the instance character; the client cannot supply its
  own research question for authority. The server extracts one structured
  research request from canonical source text and persists a unique receipt
  keyed by (instance_id,source_node_id). Repeat or concurrent requests return
  the prior completion or a pending receipt; they do not double-ingest.
* Research action means a **bounded private-corpus search**, not an Internet
  search. With the intentionally empty corpus, no_access or no_results is a
  correct outcome. Reference hits are not beliefs, sources are not
  auto-studied, and model-generated tool calls never directly promote /char or
  /world facts.
* Deliberate source study is only proposed with an articulated read/study
  intention. Study still rechecks current ACL and pinned corpus content digest.
* The HUD shows research completion under RESEARCH ACTIVITY, distinct from
  the physical scene. It labels the immediate managed goal separately from
  other active goals; it does not silently resolve contradictory commitments.

## Read-only diagnostics

    curl -sS http://127.0.0.1:8000/agent/knowledge-atlas/health | python -m json.tool

The response includes active/retired topic counts, active opaque label count,
pending Qdrant/Fuseki revisions and oldest pending ages, corpus counts,
source-linked topic coverage and current research dossier activity.

To inspect the particular rendered research request, substitute the current
instance and source node UUID from your SillyTavern/AIOS log:

    SELECT source_node_id,status,question,dossier_id,research_id,
           result,updated_at
    FROM aios.character_research_tool_request
    WHERE instance_id='<INSTANCE_UUID>'::uuid
    ORDER BY updated_at DESC LIMIT 10;

    SELECT dossier_id,question,status,cycles_completed
    FROM aios.character_research_dossier
    WHERE instance_id='<INSTANCE_UUID>'::uuid
    ORDER BY updated_at DESC LIMIT 10;

## Verification

\`tests/test_atlas_stabilization.py\` checks candidate rejection, source tag
parsing, legacy HUD isolation, and Qdrant/Fuseki acknowledgement ordering.
\`tests/ci_atlas_stabilization.py\` uses a disposable real PostgreSQL database
to ensure a changed source receipt with the same effective topic/edge identity
does not advance projection revisions, historical opaque candidates retire
exactly once, the tool ledger exists, and the health report returns coverage.
Both are included in the semantic topology CI workflow.

Live acceptance test: an authored Renamon research action produces exactly one
research tool request row, at most one corresponding dossier step for that
source node, and a compact research HUD receipt while leaving the scene's last
physical change untouched. With no authorized corpus sections it returns
no_access/no_results, not invented citations.
