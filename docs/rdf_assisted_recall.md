# RDF-assisted character recall

RDF now supplies candidate evidence to personal inquiry, background retrieval preparation,
and goal planning. It does not independently establish truth, admit memories, or complete goals.
There is no additional model call or entity extraction pass: discovery uses the cached linguistic
projection, indexed SQL term coordinates, and bounded SPARQL reads.

## Runtime behavior

- Personal inquiry discovers character entity anchors and existing SemanticAtom links. SQL
  rehydrates at most three results with current access, authority, source, and lineage checks.
  Ordinary personal retrieval remains the fallback when RDF supplies no usable evidence.
- Background prewarm discovers references once per prepared retrieval key. HUD construction
  reads only a ready, 15-second trace and revalidates it; it never queries Fuseki. Source reports
  have a separate attributed section with a maximum 300-token / 8% profile budget. Authorized
  propositions enter the normal knowledge ranking and budget selection.
- Environment goal questions can request internal recall. This lane cannot fall back to external
  reference research or request an inquiry model call. A receipt attaches source coordinates to
  the exact goal requirement. Subsequent plans rehydrate those coordinates through SQL only.
  A relevant eligible-evidence revision permits another lookup; merely advancing the chat head
  does not. Recall alone does not satisfy an environment requirement.

Entity source graphs are restricted to the character dataset and character owner. The active
chat must be an actual ancestor of the selected head, with a bounded 256-edge walk. Other
sources require an eligible acquisition in the explicit cognitive instance scope and an active
semantic admission with reflection permission. Legacy SemanticAtom reads use the existing
character epistemic graph and the same current SQL admissions. Existing world retrieval
continues separately; this pass grants no new access to world or other characters' graphs.

Source references include the projection revision, payload hash, section offsets, and matching
entity coordinates. Current section hashes and exact text spans must still match. Superseded
sources, sibling branches, revoked admissions, and changed content are discarded. Source
reports remain attributed earlier reports, not assertions about current inventory or world state.
Proposition results retain polarity, modality, and current authority.

## Deployment and diagnostics

Run the normal migration command before restarting workers. Migration
`20261023_01_rdf_assisted_recall.sql` adds indexed search coordinates and recall traces, and
queues existing entity projections for rebuilding. Until rebuilding and RDF publication finish,
those rows are excluded from recall and ordinary retrieval continues.

`aios.entity_recall_trace` records status, candidate counts, validated coordinates, scope and
query hashes, and the discovery consumer. Trace expiry controls readiness, not deletion.
The HUD frame's `rdf_retrieval` contains the trace ID and the source/proposition IDs actually
retained after selection; the same selection is logged. A partial inquiry receipt also records
its RDF reference and trace ID. These are diagnostic records, not acquisitions.

SQL operations have a 350 ms limit, Fuseki requests use an 800 ms transport timeout with no
retry, prewarm has a 2.5 s ceiling, and HUD cache reads have a 200 ms ceiling. Failure leaves
ordinary retrieval available and never establishes that a character has no resources.
Discovery is lexical (up to six terms), bounded to twelve candidate sources and propositions,
and traverses only directly connected entity relations. It does not perform global graph
reasoning, resolve every alias, or infer ownership from a mere mention.

## Verification

The recall tests run executable SPARQL through RDFLib and SQL through PostgreSQL-compatible
PGlite. They cover graph/source hash validation, ancestry and scope isolation, stale or revoked
evidence, negative propositions, Fuseki failure, ready-cache reads without network or writes,
and requirement-linked next-plan evidence. Planner and inquiry tests cover internal-only
routing and repeat suppression. These checks do not verify a running production Fuseki or SQL
installation.
