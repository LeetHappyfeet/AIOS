# AIOS

**AIOS is a persistent memory and runtime system for AI agents and characters.**

AIOS gives long-running agents continuity beyond a single prompt or chat. It maintains persistent memory, world and character state, timelines, and knowledge, then provides the active agent with a focused **HUD** containing the context it needs for the current generation.

**Want to try it first?** Open the [AIOS Google Colab demo](https://colab.research.google.com/drive/1c-eaLVuAu76JSgD4-rr65WvPFwzXA1zK?usp=sharing) for a guided demo without setting up a full local installation.

> **Status:** AIOS is experimental and under active development.
>
> **License:** AIOS is source-available proprietary software for personal use by natural persons. See the [AIOS Personal Use License 1.0](LICENSE).

## What AIOS Does

AIOS is intended for persistent assistants, role-playing characters, agents, and other applications where remembering similar text is not enough.

It keeps track of what happened, when it happened, where information came from, what belongs to the shared world, and what individual characters know or believe. The HUD turns that larger persistent state into a smaller context that a client can provide to an LLM.

Vector retrieval is part of AIOS, but AIOS is not designed as a conventional RAG system. Memory, chronology, character knowledge, world state, and deterministic state are maintained as parts of a larger runtime rather than being decided by similarity search alone.

## What's Changed

This release is a substantial update from previous versions of AIOS.

- **Better live performance.** AIOS can process multiple kinds of memory work concurrently and gives live conversation work priority over heavier background processing. Large semantic workloads should be less likely to stall an active conversation.
- **Stronger character and world memory.** AIOS more clearly separates shared world information from what an individual character has actually experienced, learned, remembered, or believes.
- **Improved long-term consistency.** New information can be reconciled with existing memory instead of simply accumulating indefinitely. This gives AIOS a better foundation for contradictions, changing beliefs, corrections, and evolving world state.
- **Shared worlds and individual perspectives.** Characters can participate in a common world timeline while retaining their own knowledge and experience history.
- **Deterministic state.** AIOS now has a separate foundation for information that should be exact rather than inferred through semantic memory, such as locations, counters, status effects, and game or simulation state.
- **More reliable HUD generation.** The runtime performs stronger readiness checks before treating memory as current enough for generation, reducing the chance of returning a HUD built from partially processed state.
- **Much easier installation.** PostgreSQL, Qdrant, and Fuseki are now managed through Docker Compose while AIOS itself remains a native Python application. First-time setup, database initialization, migrations, startup, and shutdown are handled by included scripts.
- **More validation and testing.** The project now has broader automated coverage around memory, scheduling, character knowledge, world state, HUD readiness, and fresh installations.

The overall direction is simple: AIOS is moving from an advanced retrieval and memory system toward a persistent runtime that can maintain what an agent knows, what the world contains, and how both change over time.

## First-Time Installation

AIOS currently requires:

- Python 3.10 or newer
- Docker with Docker Compose v2
- Git

Clone the repository and run the setup script:

```bash
git clone --branch AIOS-development https://github.com/LeetHappyfeet/AIOS.git
cd AIOS
bash setup.sh
```

The repository can be cloned under a different directory name if desired.

The setup script handles the rest of the first-time installation. It starts PostgreSQL, Qdrant, and Fuseki, prepares the Python virtual environment, installs Python dependencies and the required language model, initializes the database, applies current migrations, and verifies that the installation is ready to run.

When setup finishes successfully, start AIOS with:

```bash
bash run.sh
```

A healthy startup should eventually report:

```text
AIOS READY
Required services: 4/4 ready
```

## Open the Gradio Interface

Once AIOS is running, point your browser to:

```text
http://127.0.0.1:7860
```

Port **7860** is the Gradio web interface. If you are opening AIOS from another computer or phone on the same network, replace `127.0.0.1` with the IP address of the machine running AIOS, for example:

```text
http://192.168.1.50:7860
```

The AIOS API runs separately on port **8000**:

```text
http://127.0.0.1:8000
```

Most users who simply want to inspect and use AIOS should start with the Gradio interface on port 7860. Client integrations use the API on port 8000.

## Running AIOS Later

After the first installation, return to the repository and run:

```bash
bash run.sh
```

`run.sh` starts the required Docker services if necessary and launches AIOS using the environment created during setup.

Press `Ctrl+C` to stop the native AIOS processes.

To also stop PostgreSQL, Qdrant, and Fuseki without deleting their stored data:

```bash
bash stop.sh
```

AIOS data is stored in persistent Docker volumes. Do not use `docker compose down -v` unless you intentionally want to delete that data.

Optional infrastructure settings are available in `.env.example`.

## Client Integration

AIOS exposes an API for creating sessions, activating characters, ingesting new information, and retrieving the current HUD for generation.

The [MemoryVaultIngest SillyTavern extension](https://github.com/LeetHappyfeet/extension-MemoryVaultIngest) is one client that integrates AIOS with a live role-playing environment.

Additional technical documentation is available in the `docs/` directory for developers who want to work on AIOS itself or build integrations.

## License

AIOS is licensed under the **AIOS Personal Use License 1.0**. Personal use, study, experimentation, and private modification by natural persons are permitted. Commercial, organizational, institutional, hosted, and service-provider use requires a separate written license.

Earlier versions distributed under the Apache License 2.0 remain governed by the license applicable to those versions.

See [`LICENSE`](LICENSE) for the complete terms.
