# AIOS

AIOS is an epistemic runtime and memory architecture for language-model agents. It turns raw observations—chat, documents, web material, and other ingested text—into provenance-preserving temporal records, normalized claims, RDF knowledge, character-specific epistemic state, concrete runtime worlds, and finally a bounded HUD that can be consumed by either an LLM or a human-facing client.

The project has moved beyond its original “RAG sidecar” phase. The RDF/epistemic pipeline and the first complete character/world runtime are now in place. Current development is focused on the **HUD assembly layer**: deciding what a character should perceive, remember, believe, carry, care about, and be allowed to act on at a particular moment without leaking information across characters, worlds, timelines, or branches.

> **Development status:** the ingestion → DAG → claim → RDF → context-resolution → character/world runtime chain is implemented. The active development frontier is the branch-aware RPG/agent HUD and the clients that consume it.

<p align="center">
  <img src="screenshot.png" alt="AIOS Screenshot" width="800">
</p>

## What AIOS is trying to solve

Most LLM memory systems retrieve text and place it back into a prompt. AIOS instead treats memory as an epistemic problem.

A statement can be observed without being true. A source can disagree with another source. A character can know something that another character does not. A character can remember an event differently from the global state of a world. A runtime branch can diverge without rewriting its parent history. “I” must resolve to the active character, while world facts must remain attached to the correct world.

AIOS therefore separates:

- **observation** — what entered the system;
- **temporal truth** — where and when it occurred in the DAG;
- **linguistic claims** — what the text appears to assert;
- **semantic context** — what kind of claim/entity/relation it is and whose viewpoint produced it;
- **world state** — what belongs to a particular world or branch;
- **character epistemics** — what a particular character knows, believes, remembers, or has acquired;
- **runtime state** — the concrete character instance currently acting in a world;
- **presentation/attention** — the bounded HUD assembled for a human or LLM.

RAG remains useful as a similarity oracle, but vector similarity is not treated as truth and is not allowed to decide epistemic visibility by itself.

## Architecture

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

Qdrant/vector retrieval can assist candidate discovery, but IDs, provenance, world boundaries, character boundaries, and DAG position remain authoritative.

## 1. Observation and the DAG

All input begins as observation rather than fact.

The API and external ingestion paths persist an `ingest_event`, then anchor that event to a timeline in the DAG. Documents, paragraphs, and chat messages therefore share a common temporal model while retaining their source metadata.

The DAG is deliberately non-semantic. Its job is to preserve ordering, containment, identity, and provenance. Later classifiers can be replaced or improved without rewriting what was originally observed.

For chat ingestion, the source timeline remains immutable provenance. Active runtime instances advance a source-perception cursor rather than copying source messages into the concrete runtime DAG.

## 2. Claims and RDF

DAG-backed text is projected into stable document sections, split into canonical sentences, and converted into `claim_candidate` records. Claims are tentative linguistic assertions, not facts.

Claims are promoted into the Jena `/world` dataset through the liminal graph:

```text
urn:aios:world:liminal
```

Liminal means “observed and available for semantic processing,” not “true.” Contradictory claims can coexist there while retaining provenance.

The pipeline normalizes propositions, records RDF promotion receipts, classifies structural content, and performs later epistemic projections without collapsing disagreement into a single answer.

## 3. Context Resolver and semantic pivots

The Context Resolver is the bridge between generic RDF claims and the character/world engine.

It classifies claims into first-order semantic kinds including:

```text
PERSON          LOCATION        OBJECT          EVENT
MEMORY          RELATIONSHIP    BELIEF          GOAL
RULE            TRAIT           STATE           CONCEPT
ORGANIZATION    TIME            ACTION          QUANTITY
```

It also groups predicates into semantic families such as spatial, temporal, social, possession, epistemic, memory, causal, emotional, identity, descriptive, rule, goal, action, membership, and communication.

Most importantly, the resolver attaches the coordinates required to prevent epistemic leakage:

- originating `character_id`;
- character instance when one can be resolved;
- viewpoint;
- `world_id`;
- timeline and DAG node;
- epistemic scope;
- acquisition mode;
- subject/object semantic-pivot flags.

A character ID is a semantic pivot for first-person and character-relative knowledge. A world ID is the higher-level pivot for facts and state belonging to a world. The result is not one permanent RDF tree; AIOS derives context-specific trees/views from a graph according to character, world, timeline, and branch.

This allows the same proposition to be globally available as an observed claim while remaining inaccessible to a character that has never perceived or acquired it.

## 4. Character epistemics

AIOS maintains a separate character knowledge model rather than treating `/char` as a copy of `/world`.

Character epistemic state can represent knowledge, belief, memory, source acquisition, generated information, confidence/weighting, and links back to normalized propositions and concrete runtime entities. This supports cases such as:

- two characters knowing different things about the same world;
- a character remembering an event that another character never witnessed;
- a document or conversation teaching a character something;
- contradictory sources remaining visible rather than being averaged away;
- generated facts being tracked separately from observed source material.

The separation between `/world` and `/char` is fundamental: world claims describe a world context; character claims describe a character's epistemic relationship to information in that context.

## 5. Character and world runtime

The runtime turns the epistemic model into an environment an agent can inhabit.

A character identity can be activated into a concrete `character_instance` associated with a runtime world, timeline, entity, controller, and mutable runtime state. Worlds can form parent/root relationships and runtime branches, allowing a session to diverge without destroying the source world or its provenance.

Runtime support includes world entities, entity relations, world rules, character state, location, inventory/stateful objects, actions, controller identity, branching/forking, and source-perception boundaries.

Humans, LLMs, or other controllers can therefore operate through the same runtime model instead of requiring separate memory architectures.

## 6. The HUD: current development focus

The HUD is the next major layer being built on top of the completed pipeline and runtime foundations.

The canonical HUD assembler resolves the active runtime/world/DAG coordinates **before** selecting content. Context and branch eligibility are hard boundaries; relevance scoring only ranks information that is already legal for the active character to see.

The current frame can assemble sections for:

- identity and epistemic profile;
- runtime presence and world/timeline coordinates;
- current scene and nearby entities;
- physical, emotional, social, health, stamina, and energy state;
- relationships;
- inventory;
- active memories;
- knowledge and beliefs;
- goals;
- rules;
- recent perceived events;
- available actions.

The frame is deterministic and token-budgeted. It exposes both a structured JSON representation for applications and a deterministic text renderer intended to become the LLM-facing “text adventure/HUD” prompt surface.

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

The HUD should never become another unrestricted retrieval layer. Its purpose is to provide the active mind with the smallest useful, provenance-compatible view of the much larger AIOS graph.

## API highlights

The FastAPI service currently exposes the runtime and ingestion surfaces used by clients. Important routes include:

```text
POST /session
POST /ingest

POST /character/{character_id}/activate
GET  /instance/{instance_id}/state
GET  /instance/{instance_id}/frame
GET  /instance/{instance_id}/frame/text
POST /instance/{instance_id}/action
```

Additional endpoints support world entities/relations/rules, character forks/controllers, knowledge acquisition, generated and observed facts, epistemic queries, long-document ingestion, and character epistemic profiles.

The text-frame endpoint is intended for integrations such as SillyTavern or other LLM clients. The JSON frame is intended for richer interfaces that want to render the same canonical state themselves.

## Pipeline execution

AIOS uses a supervisor/runner job pipeline. The supervisor discovers eligible work and queues jobs; the runner performs individual stages. Major work now includes character discovery, world topology projection, document/claim processing, liminal RDF promotion, proposition normalization, context resolution, and epistemic projection.

This separation makes the pipeline replayable and keeps HTTP ingestion from pretending that all downstream semantic processing completed synchronously.

## Storage roles

**PostgreSQL** is the durable operational and provenance store. It contains ingest events, DAG structure, source/document projections, claims, pipeline jobs, character identities and instances, world topology, runtime state, epistemic records, and RDF processing receipts.

**Apache Jena Fuseki** provides RDF semantic workspaces. AIOS uses separate `/world` and `/char` datasets/graphs so world semantics and character epistemics can be reasoned about without conflating them.

**Qdrant** provides vector similarity/retrieval support. It is a retrieval aid, not an authority on truth, chronology, world membership, or character knowledge.

## Running the development branch

AIOS is currently under active development. The development branch is:

```bash
git switch AIOS-development
git pull
```

Install the Python requirements, configure the environment/database settings, and provide reachable PostgreSQL, Fuseki, and Qdrant services. Fuseki must have the `/world` and `/char` datasets and the AIOS ontology loaded.

From the application environment, launch AIOS with:

```bash
python -m aios_app.launch
```

The default services are:

```text
FastAPI:  http://localhost:8000
Web UI:   http://localhost:7860
```

Use the health endpoint to verify the API:

```text
GET /healthz
```

Database migrations in `migrations/` must be applied for the schema expected by the current development branch.

## Requirements

Core infrastructure:

- Python and the project dependencies in the repository;
- PostgreSQL 14+;
- Apache Jena Fuseki with `/world` and `/char` datasets;
- Qdrant for vector retrieval.

See the repository configuration, migrations, ontology files, and application modules for the exact development-state schema and service settings.

## Project direction

The original ingestion and RDF work established the system's durable memory substrate. The character engine added identity, viewpoint, epistemic separation, world topology, runtime instances, and branching. The Context Resolver connected those two halves.

The current milestone is to finish the HUD as the **attention and presentation layer** over that architecture. Once stable, clients should not need to understand the entire RDF graph or SQL schema. They should be able to activate a character, submit observations/actions, and request a bounded frame representing what that character can reasonably perceive, remember, know, believe, and do now.

That is the intended AIOS boundary: **observations go in; an epistemically valid world-and-character context comes out.**
