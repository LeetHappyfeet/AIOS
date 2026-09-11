# AIOS Architecture

This document describes the system-level architecture behind AIOS. The root [README](../README.md) is intentionally focused on what AIOS is, why it exists, and how to run it.

## The problem AIOS is solving

Most LLM memory systems retrieve text that appears relevant and place it back into a prompt. AIOS treats memory as a broader state and epistemic problem.

A statement can be observed without being true. Two sources can disagree. One character can know something another character does not. A character can remember an event differently from the current world state. A runtime branch can diverge without rewriting its parent history. First-person references must resolve to the correct viewpoint, while world facts must remain attached to the correct world and timeline.

AIOS therefore separates several concerns that are often collapsed into one retrieval layer:

- **observation** — what entered the system;
- **temporal truth** — where and when it occurred in the DAG;
- **linguistic claims** — what the text appears to assert;
- **semantic context** — what kind of claim, entity, or relation it is and whose viewpoint produced it;
- **world state** — what belongs to a particular world or branch;
- **character epistemics** — what a particular character knows, believes, remembers, or has acquired;
- **runtime state** — the concrete character instance currently acting in a world;
- **presentation and attention** — the bounded HUD assembled for an LLM or human-facing client.

Vector search is used as a similarity and candidate-discovery tool. It is not authoritative for truth, chronology, world membership, branch membership, or character knowledge.

## High-level architecture

```text
 External sources / documents                 Chat / agent interaction
              │                                        │
              └────────────────┬───────────────────────┘
                               ▼
                         ingest_event
                  immutable observed event
                               │
                               ▼
                         Temporal DAG
             ordering • containment • provenance
                               │
                               ▼
                       document_section
                               │
                               ▼
                      extracted_sentence
                               │
                               ▼
                       claim_candidate
                   untrusted S/P/O assertion
                               │
                               ▼
                    /world/liminal RDF
             semantic staging, not accepted truth
                               │
                               ▼
                       Context Resolver
          claim/entity kind • predicate family • pivots
          character_id • world_id • viewpoint • scope
                               │
                  ┌────────────┴────────────┐
                  ▼                         ▼
          /world knowledge             /char knowledge
       world/branch context       character epistemic context
                  └────────────┬────────────┘
                               ▼
                    Character/World Runtime
          instances • entities • relations • rules • state
          timelines • branches • controllers • actions
                               │
                               ▼
                         HUD Assembler
       branch eligibility → semantic routing → relevance
                               │
                  ┌────────────┴────────────┐
                  ▼                         ▼
             JSON frame                 Text frame
          application/client          LLM prompt surface
```

PostgreSQL, RDF state, IDs, provenance, world boundaries, character boundaries, and DAG position remain authoritative. Qdrant and the Semantic Index assist discovery and ranking inside those boundaries.

## Observation and the temporal DAG

All input begins as an observation rather than an accepted fact.

The API and external ingestion paths persist an `ingest_event`, then anchor that event to a timeline in the DAG. Documents, paragraphs, chat messages, and other observations therefore share a common temporal model while retaining their source metadata.

The DAG is deliberately non-semantic. Its job is to preserve ordering, containment, identity, alternatives, branches, and provenance. Semantic classifiers can later be replaced or improved without rewriting what was originally observed.

For live chat ingestion, the source timeline remains immutable provenance. Active runtime instances advance a source-perception cursor rather than copying source messages into the concrete runtime DAG.

## Claims and RDF

DAG-backed text is projected into document sections, split into canonical sentences, and converted into `claim_candidate` records. Claims are tentative linguistic assertions, not facts.

Claims can be promoted into the Jena `/world` dataset through the liminal graph:

```text
urn:aios:world:liminal
```

Liminal means observed and available for semantic processing, not accepted as true. Contradictory claims can coexist while retaining provenance.

The pipeline normalizes propositions, records RDF promotion receipts, classifies structural content, and performs later epistemic projections without collapsing disagreement into a single answer.

Ontology details and loader commands live in [rdf/ontology/readme.md](../rdf/ontology/readme.md).

## Context Resolver and semantic pivots

The Context Resolver connects generic claims to the character/world engine.

It classifies claims into first-order semantic kinds including:

```text
PERSON          LOCATION        OBJECT          EVENT
MEMORY          RELATIONSHIP    BELIEF          GOAL
RULE            TRAIT           STATE           CONCEPT
ORGANIZATION    TIME            ACTION          QUANTITY
```

Predicates are grouped into semantic families such as spatial, temporal, social, possession, epistemic, memory, causal, emotional, identity, descriptive, rule, goal, action, membership, and communication.

The resolver also attaches the coordinates required to avoid epistemic leakage:

- originating character identity;
- character instance when one can be resolved;
- viewpoint;
- world;
- timeline and DAG node;
- epistemic scope;
- acquisition mode;
- subject/object semantic-pivot flags.

A character ID acts as a semantic pivot for first-person and character-relative knowledge. A world ID is the higher-level pivot for facts and state belonging to a world. AIOS derives context-specific views from the graph according to character, world, timeline, and branch rather than treating the graph as one permanently visible tree.

The same proposition can therefore remain globally available as an observed claim while being inaccessible to a character that has never perceived or acquired it.

## Character epistemics

AIOS maintains a separate character knowledge model rather than treating `/char` as a copy of `/world`.

Character epistemic state can represent knowledge, belief, memory, source acquisition, generated information, confidence and weighting, and links back to normalized propositions and concrete runtime entities.

This allows AIOS to represent situations such as:

- two characters knowing different things about the same world;
- a character remembering an event another character never witnessed;
- a document or conversation teaching a character something;
- contradictory sources remaining visible rather than being averaged away;
- generated information being tracked separately from observed source material.

The separation between `/world` and `/char` is fundamental. World claims describe a world context; character claims describe a character's epistemic relationship to information in that context.

## Character and world runtime

The runtime turns the epistemic model into an environment an agent can inhabit.

A character identity can be activated into a concrete `character_instance` associated with a runtime world, timeline, entity, controller, and mutable runtime state. Worlds can form parent/root relationships and runtime branches, allowing a session to diverge without destroying source history or provenance.

Runtime support includes world entities, entity relations, rules, character state, location, inventory and stateful objects, actions, controller identity, branching/forking, and source-perception boundaries.

Humans, LLMs, or other controllers can therefore operate through the same runtime model instead of requiring separate memory architectures.

## The HUD

The HUD is the attention and presentation layer over the larger AIOS state.

The HUD assembler resolves the active runtime, world, timeline, branch, and source-DAG coordinates before selecting content. Eligibility is a hard boundary. Relevance scoring is only allowed to rank information after it is already legal for the active character to see.

A frame can contain sections for identity and epistemic profile, runtime presence, scene state, nearby entities, physical/emotional/social state, relationships, inventory, active memories, knowledge and beliefs, goals, rules, recent perceived events, available actions, and plugin-provided status.

The design target is:

```text
all stored knowledge
        ↓
world + branch eligibility
        ↓
character epistemic eligibility
        ↓
scene/entity relevance
        ↓
attention / token budgeting
        ↓
small actionable HUD
```

The HUD is deterministic and token-budgeted. It exposes both a structured JSON representation and a deterministic text renderer. The HUD should not become another unrestricted retrieval layer; its purpose is to provide the active mind with the smallest useful, provenance-compatible view of the much larger AIOS graph.

## Live API boundary

The canonical live-generation path is:

```text
external conversation
        ↓
POST /session
        ↓
POST /character/{character_id}/activate
        ↓
POST /ingest
        ↓
returned node_id
        ↓
POST /instance/{instance_id}/hud?through_node_id=<node_id>
        ↓
generation-consistent JSON frame + rendered text
```

`POST /instance/{instance_id}/hud` is the canonical endpoint for live clients. It can wait for the requested source DAG coordinate and returns both the structured frame and canonical rendered HUD text. The older frame and text-frame endpoints remain compatibility views and should not be treated as the preferred live-generation contract.

Other API surfaces support world entities, relations, rules, character forks/controllers, knowledge acquisition, generated and observed facts, epistemic queries, long-document ingestion, and character epistemic profiles.

## Pipeline execution

AIOS uses a supervisor/runner pipeline.

The supervisor discovers eligible work and queues jobs. The runner performs individual stages. Work includes source/document projection, claim extraction, proposition normalization, RDF promotion, semantic-frame decomposition, context resolution, topology/ownership processing, and epistemic projection.

This separation keeps HTTP ingestion durable and fast without pretending that all downstream semantic processing completed synchronously. It also makes semantic processing replayable as classifiers and architecture evolve.

## Storage roles

### PostgreSQL

PostgreSQL is the durable operational and provenance store. It contains ingest events, DAG structure, source/document projections, claims, pipeline jobs, character identities and instances, world topology, runtime state, epistemic records, semantic-engine state, HUD readiness data, and RDF processing receipts.

### Apache Jena Fuseki

Fuseki provides RDF semantic workspaces. AIOS uses separate `/world` and `/char` datasets/graphs so world semantics and character epistemics can be processed without conflating them.

### Qdrant / Semantic Index

Qdrant maintains separate semantic vector spaces for source material, propositions, and epistemic objects. It supports similarity search, candidate discovery, clustering, structural analysis, topology enrichment, and HUD attention.

The detailed Semantic Index design is documented separately in [semantic_index/README.md](../semantic_index/README.md). That subsystem intentionally treats vector geometry as advisory rather than authoritative.

## Authority rule

The most important architectural rule is that no single retrieval or classification layer is allowed to silently become truth.

Observations preserve what was seen. The DAG preserves where it happened. Claims preserve what language appears to assert. Semantic machinery proposes structure. World and character layers establish scope and ownership. Runtime state establishes the active context. The HUD exposes only the bounded portion that is relevant and permitted at that moment.

That is the intended AIOS boundary: observations go in; an epistemically valid world-and-character context comes out.
