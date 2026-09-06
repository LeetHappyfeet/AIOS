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
