# AIOS Semantic Index

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

High-confidence cluster classifications materialize `SEMANTIC_CLUSTER` nodes
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
