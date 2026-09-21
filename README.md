![AIOS](Banner.png)

# AIOS

**Persistent memory and world state for AI agents.**

AIOS gives long-running AI characters and agents continuity beyond a single prompt or chat. It remembers events, tracks changing world state, maintains what individual characters know and believe, preserves durable character identity, and retrieves the context that matters for the current moment.

Instead of treating memory as a pile of old text, AIOS maintains persistent state and produces a focused **HUD** for the active agent.

**Want to try it without installing anything?**
Open the [AIOS Google Colab demo](https://colab.research.google.com/drive/1c-eaLVuAu76JSgD4-rr65WvPFwzXA1zK?usp=sharing).

> **Status:** AIOS is experimental and under active development.
>
> **License:** AIOS is source-available proprietary software for personal use by natural persons. See the [AIOS Personal Use License 1.0](LICENSE).

## Run AIOS

AIOS currently requires:

* Python 3.10 or newer
* Docker with Docker Compose v2
* Git

For the current development branch:

```bash
git clone --branch AIOS-development https://github.com/LeetHappyfeet/AIOS.git
cd AIOS
bash setup.sh
```

The setup script:

* starts PostgreSQL, Qdrant, and Apache Jena Fuseki;
* creates the Python virtual environment;
* installs AIOS dependencies and the required spaCy model;
* loads the AIOS ontology;
* initializes the PostgreSQL database;
* applies current migrations;
* verifies the installation.

When setup completes, start AIOS with:

```bash
bash run.sh
```

A healthy startup ends with:

```text
✅ AIOS READY
   Required services: 4/4 ready
```

Open the web interface at:

```text
http://127.0.0.1:7860
```

The API is available at:

```text
http://127.0.0.1:8000
```

Verify the API with:

```bash
curl http://127.0.0.1:8000/healthz
```

Expected response:

```json
{"ok":true}
```

Press `Ctrl+C` to stop the native AIOS processes.

To also stop PostgreSQL, Qdrant, and Fuseki:

```bash
bash stop.sh
```

Stored AIOS data remains in persistent Docker volumes. Do not use `docker compose down -v` unless you intentionally want to erase those databases.

Optional infrastructure settings are documented in `.env.example`.

See [docs/installation.md](docs/installation.md) for installation details and troubleshooting.

## What Does AIOS Remember?

AIOS separates several kinds of persistent state that ordinary retrieval systems tend to mix together.

**Identity** describes who a character is. It is durable, versioned, provenance-backed, and deliberately difficult for ordinary conversation to rewrite.

**Experience** records what happened. AIOS can represent individual event occurrences and combine related events into episodes without throwing away the original evidence.

**Knowledge and belief** represent what a particular character knows, remembers, or currently accepts. Two characters can therefore inhabit the same world without automatically sharing the same information.

**World state** represents shared information and concrete runtime state independently of private character cognition.

**The HUD** selects the useful portion of all of this state for the active agent at the current point in the world and timeline.

## Why Not Just RAG?

Traditional RAG usually asks:

```text
What stored text is similar to this prompt?
```

AIOS also needs to ask:

```text
What happened?
Was this the same event or a different occurrence?
Which world did it happen in?
What does this character know?
What does this character believe?
What belongs to public world knowledge?
Who is this character supposed to be?
What is relevant right now?
```

Vector search is useful inside AIOS, but similarity does not decide truth, chronology, identity, world ownership, or character knowledge.

A simplified view is:

```text
observations
     ↓
events / knowledge / world state
     ↓
character-specific memory and belief
     ↓
relevant recall
     ↓
HUD
     ↓
LLM or human
```

The larger memory remains persistent even though only a small portion is placed into the active prompt.

## Character Identity

AIOS now maintains character identity separately from ordinary memory.

Character cards and other reference sources can provide identity material, but imported material passes through a provenance-backed identity layer rather than becoming runtime memory.

Accepted identity is compiled into a deterministic **Identity Kernel** shared by instances of the same character.

This prevents a remembered conversation, temporary mood, contradictory source, or stray observation from silently redefining who the character is.

See [docs/identity_kernel.md](docs/identity_kernel.md) for the identity architecture.

## Client Integration

The normal live-agent flow is:

```text
POST /session
        ↓
POST /character/{character_id}/activate
        ↓
POST /ingest
        ↓
POST /instance/{instance_id}/hud
        ↓
structured frame + rendered HUD context
```

The HUD is the primary generation-facing boundary. Clients do not need to understand the underlying PostgreSQL schema, RDF graphs, semantic topology, or retrieval system.

The [MemoryVaultIngest SillyTavern extension](https://github.com/LeetHappyfeet/extension-MemoryVaultIngest) is one example of a live AIOS client.

AIOS also exposes APIs for world state, character knowledge, documents, epistemic search, identity management, and runtime actions.

## Architecture

AIOS currently uses:

**PostgreSQL** for durable memory, provenance, timelines, character/world state, cognition, belief state, events, episodes, and pipeline state.

**Apache Jena Fuseki** for RDF semantic representations of world and character knowledge.

**Qdrant** for semantic candidate discovery and retrieval acceleration.

**AIOS runtime services** for ingestion, semantic processing, cognition, retrieval, character/world runtime, and HUD assembly.

The storage systems have different responsibilities. Vector similarity is never treated as the sole authority over memory or truth.

For the deeper architecture, see the documentation instead of the root README:

* [Architecture](docs/architecture.md)
* [Installation](docs/installation.md)
* [Character Identity Kernel](docs/identity_kernel.md)
* [Belief Reconciliation](docs/belief_reconciliation.md)
* [Causal Integrity](docs/causal_integrity.md)
* [Semantic Index](semantic_index/README.md)
* [Plugin System](plugins/README.md)
* [RDF Ontology](rdf/ontology/readme.md)

## Development

AIOS is under active development and its internal schema and APIs may still change.

The current development work is focused on making persistent memory behave less like search and more like an evolving cognitive/runtime system: preserving event identity, episodic continuity, character perspective, durable identity, public world knowledge, and reliable recall over long-running sessions.

Issues and regression reports are welcome, especially when accompanied by the AIOS logs and the source interaction that produced the problem.

## License

AIOS is licensed under the **AIOS Personal Use License 1.0**.

Personal use, study, experimentation, and private modification by natural persons are permitted. Commercial, organizational, institutional, hosted, and service-provider use requires a separate written license.

Earlier versions distributed under the Apache License 2.0 remain governed by the license applicable to those versions.

See [`LICENSE`](LICENSE) for the complete terms.
