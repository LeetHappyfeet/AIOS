# AIOS

**AIOS is a persistent memory and runtime system for AI agents.**

It sits between an LLM and its sources of information—conversations, documents, applications, web material, sensors, or other services—and maintains structured state about what happened, where it happened, what is believed to be true, and what each agent is allowed to know.

Instead of repeatedly searching old text and stuffing it back into a prompt, AIOS builds persistent world, character, timeline, provenance, and memory state. It then produces a bounded **HUD** containing the information an active human or AI agent needs right now.

```text
observations → persistent memory/world model → agent-specific HUD → LLM or human
```

> **Development status:** AIOS is experimental and under active development. The ingestion, temporal DAG, semantic processing, character/world runtime, and HUD pipeline are operational. Current work is focused on semantic quality, HUD assembly, and client integrations.

> **License:** AIOS is **source-available proprietary software**, not open-source software. The current code is licensed for **personal use by natural persons only** under the [AIOS Personal Use License 1.0](LICENSE). Commercial, organizational, institutional, hosted, and service-provider use requires a separate written license.

<p align="center">
  <img src="screenshot.png" alt="AIOS Screenshot" width="800">
</p>

## What is AIOS?

AIOS is intended for persistent agents, role-playing characters, assistants, automation systems, and other applications where an LLM needs continuity beyond a single prompt or chat window.

A conventional retrieval system usually asks:

> What old text looks similar to the current prompt?

AIOS needs to answer a different set of questions:

> What happened? Which world and timeline are active? What does this character know or believe? Where did that information come from? Is the agent allowed to see it? What matters right now?

That distinction matters when memory is more complicated than a searchable pile of text. Two sources can disagree. One character can know something another character does not. A branch can diverge without rewriting its parent history. An observation can be stored without being accepted as true.

AIOS keeps those distinctions instead of collapsing them into retrieval results.

## How is this different from RAG?

Retrieval-augmented generation is useful inside AIOS, but retrieval is not the authority over memory or truth.

Vector search helps AIOS discover similar source material, propositions, semantic structure, and potentially relevant memories. PostgreSQL, RDF state, provenance, timelines, world boundaries, character boundaries, and runtime state decide what the information actually belongs to and whether the active agent can use it.

In short:

```text
Traditional RAG:
prompt → similarity search → retrieved text → prompt

AIOS:
observation → chronology/provenance → semantic structure
            → world + character state → bounded HUD → agent
```

For the deeper design, see [docs/architecture.md](docs/architecture.md).

## Quick start

AIOS is currently a development environment rather than a packaged end-user application.

You need Python 3.10+, PostgreSQL 14+, Apache Jena Fuseki, and Qdrant. PostgreSQL, Fuseki, and Qdrant must already be reachable before AIOS starts.

### 1. Clone the development branch

The repository root is the `aios_app` Python package, so clone it into a directory named `aios_app` and run Python from its parent directory:

```bash
git clone --branch AIOS-development https://github.com/LeetHappyfeet/AIOS.git aios_app
```

Your working directory should look roughly like this:

```text
workspace/
├── .venv/
└── aios_app/
    ├── __init__.py
    ├── launch.py
    └── ...
```

### 2. Create the Python environment

From the parent directory of `aios_app`:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r aios_app/requirements.txt
python -m spacy download en_core_web_sm
```

### 3. Configure the external services

At minimum, point AIOS at PostgreSQL, Fuseki, and Qdrant:

```bash
export AIOS_DB_DSN='postgresql://USER:PASSWORD@127.0.0.1:5432/DATABASE'
export AIOS_FUSEKI_BASE_URL='http://127.0.0.1:3030'
export AIOS_QDRANT_URL='http://127.0.0.1:6333'
```

Fuseki must provide the AIOS `/world` and `/char` datasets and have the required ontology graphs loaded.

### 4. Launch AIOS

```bash
python -m aios_app.launch
```

The launcher automatically applies the current PostgreSQL migrations, checks database readiness, and then starts the AIOS services in readiness-gated stages.

A healthy startup ends with output similar to:

```text
✅ AIOS READY
   Required services: 4/4 ready
   Optional services: 2/2 ready
```

### 5. Verify it

```bash
curl http://127.0.0.1:8000/healthz
```

Expected response:

```json
{"ok":true}
```

Default local endpoints:

```text
API:    http://127.0.0.1:8000
Web UI: http://127.0.0.1:7860
```

For service setup, environment variables, package-layout notes, and troubleshooting, see [docs/installation.md](docs/installation.md).

## How AIOS works

The basic system can be understood without knowing the full database or RDF design:

```text
External sources / chat / documents
                │
                ▼
           Observation
                │
                ▼
          Temporal DAG
      chronology + provenance
                │
                ▼
       Semantic processing
       claims + relationships
                │
        ┌───────┴────────┐
        ▼                ▼
   World context    Character knowledge
        └───────┬────────┘
                ▼
       Character runtime
                │
                ▼
              HUD
       relevant permitted state
                │
        ┌───────┴────────┐
        ▼                ▼
       LLM             Human/UI
```

The important separation is between **what was observed**, **what the world contains**, **what a particular character knows or believes**, and **what the active agent should receive right now**.

The HUD is the final attention boundary. It is designed to expose a small, useful view of the much larger AIOS memory/world model rather than dumping unrestricted retrieval results into a prompt.

## Live client flow

The intended live-generation path is:

```text
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
JSON frame + canonical rendered HUD text
```

`POST /instance/{instance_id}/hud` is the canonical live HUD endpoint. It coordinates the HUD with the requested source DAG position and returns both the structured frame and the rendered text surface a client can inject into an LLM prompt.

Older frame and text-frame endpoints remain for compatibility, but live integrations should use the HUD endpoint.

The [MemoryVaultIngest SillyTavern extension](https://github.com/LeetHappyfeet/extension-MemoryVaultIngest) is one example of a client using AIOS as an external memory/runtime service.

## Major components

**PostgreSQL** stores durable operational state, provenance, DAG structure, pipeline state, character/world runtime state, and epistemic records.

**Apache Jena Fuseki** provides RDF semantic workspaces for world and character knowledge.

**Qdrant / Semantic Index** provides similarity search and semantic structure discovery. Vector results are advisory; they do not independently decide truth, ownership, or visibility.

**Supervisor + Pipeline Runner** advance observations through asynchronous semantic processing.

**Character/World Runtime** maintains active instances, worlds, branches, state, entities, rules, actions, and source-perception boundaries.

**HUD Assembler** converts the much larger stored state into a bounded context for an active agent.

## Documentation

- [Architecture](docs/architecture.md) — DAG, claims, RDF, context resolution, epistemics, runtime, HUD, pipeline, and storage boundaries.
- [Development installation](docs/installation.md) — environment setup, service configuration, launch behavior, and troubleshooting.
- [Semantic Index](semantic_index/README.md) — Qdrant roles, clustering, classification, reconciliation, and semantic topology.
- [Plugin system](plugins/README.md) — HUD/plugin provider architecture.
- [RDF ontology](rdf/ontology/readme.md) — canonical ontology graphs and Fuseki loading notes.

## Project direction

AIOS is moving toward a boundary where clients do not need to understand the full SQL schema, RDF graph, semantic engine, or memory pipeline.

A client should be able to establish an identity/session, submit observations and actions, activate an agent, and request a bounded frame representing what that agent can reasonably perceive, remember, know, believe, and do at that point in the world and timeline.

That is the intended AIOS contract:

**observations go in; an epistemically valid world-and-character context comes out.**

## License

AIOS is licensed under the **AIOS Personal Use License 1.0**, a proprietary source-available license. It permits personal use, study, experimentation, and private modification by natural persons. Commercial, organizational, institutional, hosted, and service-provider use is not permitted without a separate written license.

AIOS was previously distributed under the Apache License 2.0. Rights validly granted for earlier versions or commits remain governed by the license applicable to those versions; the current license does not retroactively revoke earlier grants.

See [`LICENSE`](LICENSE) for the complete terms.
