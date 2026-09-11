# Running AIOS Development

This guide describes the current development layout. AIOS is under active development, so installation is still closer to a developer environment than a packaged end-user application.

## Requirements

AIOS currently expects:

- Python 3.10+;
- PostgreSQL 14+;
- Apache Jena Fuseki;
- Qdrant;
- the Python dependencies in `requirements.txt`;
- the spaCy `en_core_web_sm` language model.

PostgreSQL, Fuseki, and Qdrant are external services and must be reachable before AIOS starts.

## Repository/package layout

The repository root is the Python package itself. Imports use the package name `aios_app`, so the simplest development checkout is to clone the repository into a directory named `aios_app` and run Python from its parent directory.

For example:

```text
workspace/
├── .venv/
└── aios_app/
    ├── __init__.py
    ├── launch.py
    ├── main.py
    └── ...
```

From `workspace/`, `python -m aios_app.launch` can import the package normally.

## Clone the development branch

From the directory that will contain the checkout:

```bash
git clone --branch AIOS-development https://github.com/LeetHappyfeet/AIOS.git aios_app
```

If the repository is already cloned:

```bash
cd aios_app
git switch AIOS-development
git pull
cd ..
```

## Create the Python environment

From the parent directory of `aios_app`:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r aios_app/requirements.txt
python -m spacy download en_core_web_sm
```

The spaCy model is a separate download. Installing the `spacy` Python package alone does not install `en_core_web_sm`.

## Configure services

AIOS reads configuration from environment variables and `.env` through `python-dotenv`.

The most important service settings are:

```bash
export AIOS_DB_DSN='postgresql://USER:PASSWORD@127.0.0.1:5432/DATABASE'
export AIOS_FUSEKI_BASE_URL='http://127.0.0.1:3030'
export AIOS_QDRANT_URL='http://127.0.0.1:6333'
```

The API defaults to:

```bash
export AIOS_API_HOST='0.0.0.0'
export AIOS_API_PORT='8000'
```

Additional pipeline, worker, timeout, embedding, and semantic-engine settings can be overridden through environment variables, but the defaults are normally appropriate for an initial development run.

## PostgreSQL

`AIOS_DB_DSN` must point to a PostgreSQL database that AIOS is allowed to migrate.

The launcher automatically runs:

```bash
python -m aios_app.migrate
python -m aios_app.db_check
```

before starting service processes. You normally do not need to run those commands separately unless diagnosing database setup.

Current migrations live in `migrations/`. Older superseded migrations are retained under `migrations/legacy/` for historical reference and upgrade compatibility logic.

## Fuseki

Fuseki must be reachable at `AIOS_FUSEKI_BASE_URL` and provide the AIOS RDF datasets used by the runtime, including `/world` and `/char`.

The canonical ontology graphs and loader examples are documented in [../rdf/ontology/readme.md](../rdf/ontology/readme.md).

AIOS treats RDF as part of the semantic/epistemic architecture, not as the sole operational database. PostgreSQL remains the durable operational and provenance store.

## Qdrant

Qdrant is used by the Semantic Index. The default endpoint is:

```text
http://127.0.0.1:6333
```

The Semantic Index service can be optional during startup, so a failure there may produce an `AIOS READY — DEGRADED` state while required core services remain available.

See [../semantic_index/README.md](../semantic_index/README.md) for its architecture and collection roles.

## Launch AIOS

From the parent directory of `aios_app`, with the virtual environment active:

```bash
python -m aios_app.launch
```

The launcher performs database preflight checks and then starts services in readiness-gated stages. Required core services include the accumulator, supervisor, pipeline runner, and API. The UI and Semantic Index are currently treated as optional services by the launcher.

A healthy startup ends with output similar to:

```text
✅ AIOS READY
   Required services: 4/4 ready
   Optional services: 2/2 ready
   API: http://0.0.0.0:8000
```

If an optional service fails its readiness check, startup can instead report:

```text
✅ AIOS READY — DEGRADED
```

## Verify the API

From the same machine:

```bash
curl http://127.0.0.1:8000/healthz
```

Expected response:

```json
{"ok":true}
```

Default local endpoints are:

```text
API:    http://127.0.0.1:8000
Web UI: http://127.0.0.1:7860
```

When accessing AIOS from another device on the LAN, replace `127.0.0.1` with the AIOS host's LAN address and ensure the relevant firewall rules allow the port.

## Common startup problems

### `No module named aios_app`

Run `python -m aios_app.launch` from the parent directory of the checkout/package, not from inside the package directory, and make sure the checkout directory is named `aios_app` or otherwise available on `PYTHONPATH` as that package.

### spaCy cannot find `en_core_web_sm`

Install the model inside the active virtual environment:

```bash
python -m spacy download en_core_web_sm
```

### PostgreSQL readiness fails

Check `AIOS_DB_DSN`, verify the server is reachable, and confirm the configured user can create/update the AIOS schema through the migration runner.

You can run the checks directly while troubleshooting:

```bash
python -m aios_app.migrate
python -m aios_app.db_check
```

### Fuseki or Qdrant is unavailable

Verify the configured endpoints first. Fuseki also requires the expected datasets and ontology graphs; a listening HTTP port alone is not enough for semantic processing to operate correctly.

## Development notes

The current branch is not yet distributed as a conventional Python package or single-container application. The commands above describe the repository's actual development layout rather than hiding that constraint behind an incomplete one-line install command.

For the system design after startup, see [architecture.md](architecture.md).
