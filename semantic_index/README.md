# AIOS Semantic Index

Topic Atlas V1 (provisional subjects, source receipts, scoped Qdrant vectors and
separate Fuseki topic-navigation graphs) is documented in
[Knowledge Topic Atlas V1](../docs/knowledge_topic_atlas_v1.md). It is a
discovery catalogue, not semantic admission or an asserted RDF ontology.


This subsystem replaces the former monolithic RAG sidecar.

Qdrant is used in three distinct semantic roles:

1. Source index: source/document/chat sections for provenance-aware similarity.
2. Proposition index: normalized propositions for candidate equivalence, conflict,
   clustering, topology enrichment and semantic structure analysis.
3. Epistemic index: character/world-owned semantic objects used as high-recall
   retrieval seeds for the HUD.

The semantic structure pass consumes proposition geometry to produce advisory
neighbor candidates. Those candidates may support later clustering, pruning,
split and branch analysis, but vector similarity never grants truth, world
membership, branch membership or character knowledge.

PostgreSQL, RDF, DAG lineage, semantic topology and the character/world runtime
remain authoritative.


## Semantic clustering

The structure engine now clusters the proposition-neighbor graph without
promoting vector geometry into authoritative semantics.

Clustering uses two stages:

- strong edges at `AIOS_SEMANTIC_CLUSTER_CORE_THRESHOLD` form disjoint cores;
- unclaimed propositions may attach as fringe only when they have at least
  `AIOS_SEMANTIC_CLUSTER_MIN_ATTACH_LINKS` edges above the attach threshold.

This prevents a single weak semantic bridge from collapsing two dense regions
into one connected component.

Each run persists:

- run-scoped cluster snapshots plus a stable member-set `cluster_key`;
- core/fringe memberships and member affinity;
- density, cohesion, boundary strength and separation;
- dominant topics, subjects, predicates, claim kinds and predicate families;
- source, world and timeline distributions and temporal span;
- cross-cluster boundary statistics;
- unclaimed/isolated propositions as advisory outlier candidates.

Clusters remain `candidate` objects. A later resolver must decide whether a
boundary represents a topic split, temporal/state transition, source narrative
split, contradiction cluster, experiential branch, world branch, or no
meaningful split.


## Semantic classification

The semantic structure pipeline now interprets vector geometry in three stages:

1. **Neighbor relation classification** labels proposition pairs as
   `EQUIVALENT`, `REFINES`, `CONTRADICTS`, `SAME_TOPIC`,
   `SAME_EVENT`, `RELATED`, or `UNRESOLVED`.
2. **Cluster classification** labels dense regions as topic, state, event,
   memory, belief, rule, goal, mixed, or unresolved regions.
3. **Boundary classification** interprets cluster separation as
   `SAME_REGION`, `TOPIC_SPLIT`, `TEMPORAL_TRANSITION`,
   `STATE_TRANSITION`, `NARRATIVE_SPLIT`,
   `CONTRADICTION_CLUSTER`, `EXPERIENTIAL_BRANCH_CANDIDATE`,
   `WORLD_BRANCH_CANDIDATE`, or `UNRESOLVED`.

Existing proposition-conflict receipts from normalization are reused as strong
pairwise evidence. Contradiction relations are retained for boundary analysis
but are forbidden from forming cluster cores, attaching fringe members, or
inflating cluster cohesion.

The classifier combines vector similarity with topic/subject/predicate overlap,
claim and predicate families, temporal overlap/separation, source distribution,
world/timeline distribution, character-instance lineage, and cross-cluster
conflicts. Missing metadata is neutral rather than treated as evidence of
separation.

All labels remain advisory. In particular, branch-candidate classifications do
not create worlds or experiential branches automatically.


## Reconciliation and RDF promotion

The semantic engine now has a reconciliation layer between advisory vector
classification and authoritative runtime structures.

The flow is:

```text
Qdrant neighbors
    -> pairwise semantic relations
    -> contradiction-aware clusters
    -> cluster/boundary classifications
    -> scope-safe reconciliation
    -> semantic_topology_edge / SEMANTIC_CLUSTER nodes
    -> RDF topology reprojection
```

Promotion is deliberately scope constrained. Two propositions can only receive
a reconciled topology relation when both already exist in the same authorized
topology scope. Character scopes are further partitioned by
`character_instance_id`, preventing vector inference from bridging sibling
experiential branches.

High-confidence pairwise relations may become derived topology edges:

- `semantic_equivalent`
- `semantic_refinement`
- `semantic_contradicts`
- `semantic_same_topic`
- `semantic_same_event`

Accepted `SAME_EVENT` relations resolve into canonical `semantic_event` records and EVENT topology nodes with `semantic_event_evidence` membership edges; they are not materialized as proposition-to-proposition identity edges.\n\nHigh-confidence cluster classifications materialize `SEMANTIC_CLUSTER` nodes
inside each scope that contains at least two members of that cluster. Cluster
boundaries then become derived semantic edges such as `state_transition`,
`temporal_transition`, `topic_boundary`, `narrative_boundary`, and
`contradiction_boundary`.

Branch classifications remain proposals. They create
`semantic_branch_candidate` records and `possible_*_branch` topology edges,
but never call the runtime world/instance branching code.

Every reconciled object receives a `semantic_reconciliation_receipt`.
Topology edges record `inference_source`, `inference_status`, confidence,
and classifier metadata. The same provenance is serialized into the scope's
Fuseki topology graph as reified edge records.

If Fuseki is temporarily unavailable, PostgreSQL reconciliation remains
durable. Receipts without an RDF dataset/graph are retried by the Semantic
Index service on subsequent passes.

The authority rule remains unchanged: vector geometry discovers structure;
classifiers interpret it; reconciliation may enrich derived topology/RDF; only
the existing SQL/RDF epistemic and runtime layers decide truth, ownership,
visibility, and actual branch creation.

## Neighborhood inspection

The **Semantic neighborhoods** control-plane tab is a read-only diagnostic. List
current clusters, then enter a cluster ID; or choose `epistemic_objects_v1` and
paste Qdrant point IDs. Set neighbors to expand around one seed. The API is:

- `GET /semantic/clusters?limit=50&offset=0`
- `POST /semantic/inspect`

Example request:

```json
{"collection":"epistemic_objects_v1","point_ids":["<point UUID>"],"neighbors":100,"limit":200}
```

The response contains canonical propositions, ownership payloads, raw extracted
claims, original sentences, source sections/messages, semantic frames, all paged
observation contexts, and internal/boundary relation pairs with both texts,
structural differences and verifier explanations. These are operator diagnostics;
they do not grant character knowledge or alter truth, ownership or topology.
Validation status is separate from advisory relation labels.

Ownership points are counted separately from distinct propositions. Medoids use
original vectors and one deterministically selected ownership vector per
proposition, not projected display coordinates or ownership frequency. They are
exact for the returned vector set, restricted to core candidates for stored
clusters; paged cluster samples and missing vectors are explicitly reported.
Diverse exemplars, contradictory endpoints and fringe IDs are also returned.
Medoids are calculated on demand and are not new assertions or replacement
memories. Existing region/edge labels remain the types for derived groups;
behavioral patterns and additional automatic collapse are deferred pending
inspection of evidence.

Requests return at most 512 points and 2,000 edges, prioritize contradiction
pairs, and report edge truncation. Evidence uses a per-proposition limit/offset
(default 5, maximum 20), source/message excerpts are limited to 12,000 characters
with original lengths, and each observation returns up to 32 frames plus a total
frame count. Missing points and missing SQL propositions are explicit.

Clustering v4 prevents known contradictory endpoints from sharing a core or
attaching to the same fringe through indirect positive paths. Cluster metadata
counts distinct proposition/context-value pairs across observations rather than
selecting the last observation. Rebuilds track classifier inserts and validation
evaluation/staleness as well as geometry, with a 30-second minimum interval
(`AIOS_SEMANTIC_CLUSTER_MIN_INTERVAL_SECONDS`). Reconciliation timestamp writes
alone do not retrigger clustering.

New or re-indexed proposition vectors receive bounded neighbor discovery.
New arrivals discover their older peers; unchanged historical nodes are not
periodically searched again. Successful search receipts remain valid until
that proposition vector changes.

Choose a current cluster from the dropdown after listing clusters. With a
positive neighbor count the UI first inspects the cluster, selects its sampled
medoid, and expands around that actual Qdrant point. The selected seed and method
are shown in the details. A cluster ID copied from the displayed current list
into the point field is recognized as a cluster ID. Clicking a member row still
selects a specific seed, clears the cluster field, and resets the member offset.
For epistemic objects the selected point ID is distinct from the proposition ID.
Validation and API errors are displayed in the result details without throwing
Gradio exceptions; the neighbor count alone cannot identify a cluster or seed.


### Incremental ingestion and explicit region analysis

The independent topology worker runs neighbor discovery, relation classification,
targeted validation, verified pair reconciliation, stale cluster cleanup, and
RDF catch-up. It does not rebuild clusters or materialize global outliers.
Validation drains once per cycle even when no new relation was classified.
Package imports no longer replace classifier/reconciler functions.

For deliberate maintenance or inspection, the existing global analysis remains
available as a one-shot command (avoid running another region analysis at the
same time):

```bash
python -m aios_app.semantic_index.cli --analyze-regions-once
```

This command still analyzes the full eligible graph; bounded scope selection
and incremental candidate queues are follow-up work. Existing cluster results
remain inspectable, and stale derived pivots are retired during normal ingestion.
The scheduling migration adds retry/classification receipts and lookup indexes.
Quarantine retains its existing deletion behavior, serialized against vector
writes by a shared advisory lock. Raw observations and propositions remain intact.

Independent environment budgets use these defaults:

| Setting | Default with index batch 64 |
| --- | ---: |
| `AIOS_SEMANTIC_NEIGHBOR_BATCH_SIZE` | 8 |
| `AIOS_SEMANTIC_RELATION_BATCH_SIZE` | 16 |
| `AIOS_SEMANTIC_VALIDATION_BATCH_SIZE` | 256 |
| `AIOS_SEMANTIC_RECONCILIATION_BATCH_SIZE` | 64 |

Completed nonempty or slow stages log their count and elapsed time. Relation
classification additionally logs candidate-query, classification, and write
durations. These timings distinguish SQL cost from the separate validation drain.


### Vector priority and independent topology maintenance

Normal `python -m aios_app.launch` starts both workers automatically, in order:

- **Semantic Index** owns the embedding model and query RPC. It processes newest
  pending proposition vectors, semantic admission, overdue admission repair,
  eligible frame vectors, character/world vectors, and a small source batch.
  Cold corpus work runs last, only when the main vector streams are not saturated.
- **Semantic Topology** starts after the vector backend is ready. It reuses the
  existing collection dimensions and loads no embedding model. Neighbor discovery,
  classification, validation, reconciliation, RDF catch-up, and quarantine run
  independently of embedding/admission.

For manual operation, run the existing vector command and a separate terminal:

```bash
python -m aios_app.semantic_index.cli --topology-only
```

The launcher reports `semantic` and `topology` independently. A topology timeout
does not stop the vector worker or prevent the remaining topology stages from
draining already verified work. Failed topology stages halve their next batch
down to one and back off; completed rows keep durable receipts. SQL statements
are cancelled at their configured deadlines. Stage budgets are cooperative:
synchronous embedding/RDF/Qdrant calls finish or reach their HTTP timeout before
an asyncio deadline can yield. Qdrant HTTP calls use a five-second timeout;
the topology worker uses a five-second Fuseki timeout without internal retries.

| Setting | Default |
| --- | ---: |
| `AIOS_SEMANTIC_INDEX_BATCH_SIZE` | 64 |
| `AIOS_SEMANTIC_ADMISSION_BATCH_SIZE` | 8 |
| `AIOS_SEMANTIC_BACKGROUND_BATCH_SIZE` | 8 |
| `AIOS_SEMANTIC_NEIGHBOR_K` | 12 |
| `AIOS_SEMANTIC_VECTOR_SQL_SECONDS` | 10 |
| `AIOS_SEMANTIC_ADMISSION_STAGE_SECONDS` | 10 |
| `AIOS_SEMANTIC_TOPOLOGY_SQL_SECONDS` | 5 |
| `AIOS_SEMANTIC_TOPOLOGY_STAGE_SECONDS` | 10 |

All embedding streams and pending admissions select newest first with stable
tie breakers. Continuous new arrivals can delay historical backfill. Overdue
admission repair retains oldest-first ordering so stalled evidence can progress.

Neighbor search applies its score floor in Qdrant and checks admitted evidence
in a single bounded SQL lookup. A hit needs a shared subject, object, topic, or
actor/target reversal before becoming a typed-edge candidate. This intentionally
reduces purely thematic connections; vector retrieval still has those vectors.
Opposing claims with the same subject/object remain discoverable. Unchanged
scores do not rewrite candidates or retrigger validation.

Classifier batches are materialized **before** occurrence enrichment. Cheaply
unsupported pairs are marked `culled`, with a reason, without a semantic assertion.
Pairs awaiting admitted context defer for 30 seconds instead of pinning the
newest batch. Classification receipts record the classifier version so upgrades
can revisit existing candidates. Frames need resolved standalone interpretations
before new frame-vector indexing; their underlying evidence is retained.

The shadow participation policy remains observational and does not control any
of these production gates.
