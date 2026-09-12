# AIOS

**AIOS is a persistent memory and runtime system for AI agents.**

AIOS sits between an LLM and sources such as conversations, documents, applications, web material, or other services. It keeps persistent world, character, timeline, provenance, and memory state, then gives an active agent a bounded **HUD** containing the information it needs right now.

```text
observations → persistent memory/world state → agent HUD → LLM or human
```

> **Status:** AIOS is experimental and under active development.
>
> **License:** AIOS is source-available proprietary software for personal use by natural persons. See the [AIOS Personal Use License 1.0](LICENSE).

## Install AIOS

AIOS runs its Python application natively and uses Docker Compose for PostgreSQL, Apache Jena Fuseki, and Qdrant.

Requirements:

- Python 3.10+
- Docker with Docker Compose

Clone AIOS into a directory named `aios_app`, then run the setup script:

```bash
mkdir -p ~/AIOS-workspace
cd ~/AIOS-workspace

git clone --branch AIOS-development https://github.com/LeetHappyfeet/AIOS.git aios_app
cd aios_app
bash setup.sh
```

`setup.sh` checks Python and Docker, starts the Compose infrastructure, waits for PostgreSQL, Qdrant, and Fuseki, loads the canonical AIOS ontology, creates the native Python virtual environment in `~/AIOS-workspace/.venv`, installs the Python requirements and spaCy model, then runs the current AIOS database migrations and database check.

The default PostgreSQL connection remains:

```text
postgresql://postgres:postgres@127.0.0.1:5432/postgres
```

The database/network services are bound to localhost by default.

## Run AIOS

After first-time setup:

```bash
cd ~/AIOS-workspace/aios_app
bash run.sh
```

`run.sh` makes sure the Compose infrastructure is running and then starts the native AIOS launcher with the existing virtual environment.

A healthy startup ends with output similar to:

```text
✅ AIOS READY
   Required services: 4/4 ready
```

Default local endpoints:

```text
API:    http://127.0.0.1:8000
Web UI: http://127.0.0.1:7860
```

Press `Ctrl+C` in the AIOS terminal to stop the native AIOS processes.

To stop PostgreSQL, Qdrant, and Fuseki without deleting their stored data:

```bash
bash stop.sh
```

The Compose data lives in persistent named Docker volumes. Do not use `docker compose down -v` unless you intentionally want to delete the stored AIOS data.

Optional infrastructure settings are documented in `.env.example`.

<p align="center">
  <img src="screenshot.png" alt="AIOS Screenshot" width="800">
</p>

## What does AIOS do?

AIOS is designed for persistent agents, role-playing characters, assistants, automation systems, and other applications where an LLM needs continuity beyond one prompt or chat window.

Unlike a simple retrieval system, AIOS keeps track of distinctions such as:

- what was observed versus what is accepted as world state;
- what one character knows versus what another character knows;
- where information came from and when it occurred;
- which world, timeline, or branch information belongs to;
- what the active agent is actually allowed to receive.

The final output is a **HUD**: a small, relevant view of the much larger stored state that can be consumed by an LLM or human-facing client.

## AIOS and RAG

AIOS can use vector retrieval, but retrieval does not decide truth, ownership, chronology, or visibility. Vector search helps find candidates; AIOS's persistent state and runtime decide what belongs in the active agent's context.

For the full design, see [docs/architecture.md](docs/architecture.md).

## Using AIOS from a client

The intended live path is:

```text
POST /session
        ↓
POST /character/{character_id}/activate
        ↓
POST /ingest
        ↓
POST /instance/{instance_id}/hud?through_node_id=<node_id>
        ↓
agent HUD
```

`POST /instance/{instance_id}/hud` is the canonical live-generation endpoint. It returns both structured HUD data and canonical rendered text.

The [MemoryVaultIngest SillyTavern extension](https://github.com/LeetHappyfeet/extension-MemoryVaultIngest) is one example of an AIOS client.

## Documentation

- [Development installation](docs/installation.md) — Compose infrastructure, environment variables, launch behavior, and troubleshooting.
- [Architecture](docs/architecture.md) — DAG, provenance, claims, RDF, epistemics, runtime, HUD, pipeline, and storage boundaries.
- [Semantic Index](semantic_index/README.md) — Qdrant, semantic structure, clustering, classification, and reconciliation.
- [Plugin system](plugins/README.md) — HUD/plugin provider architecture.
- [RDF ontology](rdf/ontology/readme.md) — canonical ontology graphs and Fuseki loading notes.

## License

AIOS is licensed under the **AIOS Personal Use License 1.0**. Personal use, study, experimentation, and private modification by natural persons are permitted. Commercial, organizational, institutional, hosted, and service-provider use requires a separate written license.

Earlier versions distributed under the Apache License 2.0 remain governed by the license applicable to those versions.

See [`LICENSE`](LICENSE) for the complete terms.