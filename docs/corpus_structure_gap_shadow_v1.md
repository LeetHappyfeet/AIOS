# Corpus Structure Gap Discovery V1 (shadow / manual)

This pass adds **no second-LLM calls**. It also does not modify HUD selection,
character memory, RDF, Qdrant, topic relations, or source admission.

## Install

Apply the normal migration runner, including
`migrations/current/20261111_01_corpus_structure_gap_shadow.sql`, before
starting the Gradio UI. Restart Gradio so the automatically registered
**Corpus Structure Gaps** tab appears.

Select an existing enabled domain and click **Scan for structural gaps**.
Each scan is bounded to the newest 25–500 indexed eligible sections. No
background scans run unless the operator clicks the button.

## Candidate types

- `semantic_unresolved_pair`: a pair of catalog topics shares high-similarity
  vector-candidate links in at least **two distinct corpus documents** within
  the same explicitly assigned knowledge domain, while there is no verified
  Topic Atlas relation in either direction. Threshold: 0.78. A document is a
  source unit, **not proof of independent authorship**. Candidate pairs are
  capped at 100 persisted per scan.
- `dense_sparse_reference`: an eligible section has at least 800 characters,
  a complete ready reference parse, six or more provisional entity anchors,
  and at most one extracted relation. This is a diagnostic for possible
  parser coverage gaps, **not proof of a missing fact**.

Candidate evidence stores source IDs, document IDs, vector-link fingerprints,
reference projection revisions and deterministic signals. A subsequent scan
marks candidates stale when their recorded sections disappear, change their
projection revision, or lose ready reference projections.

A scan requires matching vector discovery and topic-link receipts and a ready
reference RDF projection for the topic-pair path. If corpus projections are
behind, the scanner intentionally reports few or zero candidates. This is
not proof of complete ontology coverage.

## Constraints / interpretation

- The scanner is **sample based** and is not an exhaustive global audit.
- No vector-neighbor data beyond existing candidate topic links is queried,
  so relationships discarded by the top-four link cap cannot be discovered
  in this first pass.
- The graph absence test inspects `knowledge_topic_relation` records with
  `status='verified'`. It is not a full Fuseki RDF graph reachability audit.
- Revisions are projection receipts, not a synchronous cross-backend
  checksum of all live SQL content. Before enabling later automatic inference,
  require source snapshot revalidation against current content/metadata.
- Domain ACL and author/restriction policies are **not** made available to any
  model by this pass. A later inference worker must enforce source visibility
  before constructing its input package.
- Large domains, restrictive policies and partial projection backlogs may
  produce no candidates. Check corpus discovery/reference readiness first.

## Tests

```bash
pytest -q tests/test_corpus_structure_gaps.py
```

No database/server integration test is shipped in this pass; the operator
should verify migration and run a bounded scan in a non-production database
before relying on the diagnostics.

## Suggested next experiment

Measure candidate counts per domain and inspect whether the strongest pair
candidates reveal genuinely absent typed relationships. Only then authorize
a separate `corpus_structure_enrichment` inference class with bounded input,
request receipts, source-coordinate validation and hard token/CPU budgets.
