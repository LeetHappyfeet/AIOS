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
