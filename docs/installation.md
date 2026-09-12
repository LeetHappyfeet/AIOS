# Running AIOS Development

AIOS keeps its Python application native while Docker Compose manages the external storage/network services. This gives a repeatable local installation without forcing the Python runtime, NLP models, or AIOS worker processes into containers.

## Requirements

You need:

- Python 3.10+;
- Docker with Docker Compose;
- the Python dependencies in `requirements.txt`;
- the spaCy `en_core_web_sm` language model.

Docker Compose supplies:

- PostgreSQL;
- Apache Jena Fuseki;
- Qdrant.

## Repository/package layout

The repository root is the Python package itself. Imports use the package name `aios_app`, so the simplest development checkout is to clone the repository into a directory named `aios_app` and run Python from its parent directory.

```text
workspace/
├── .venv/
└── aios_app/
    ├── compose.yaml
    ├── docker/
    ├── __init__.py
    ├── launch.py
    └── ...
```

## Clone the development branch

```bash
mkdir -p ~/AIOS-workspace
cd ~/AIOS-workspace
git clone --branch AIOS-development https://github.com/LeetHappyfeet/AIOS.git aios_app
```

If the repository is already cloned:

```bash
cd ~/AIOS-workspace/aios_app
git switch AIOS-development
git pull
```

## Start PostgreSQL, Fuseki, and Qdrant

From the repository directory:

```bash
cd ~/AIOS-workspace/aios_app
docker compose up -d --build
```

This single Compose operation:

1. starts PostgreSQL;
2. starts Qdrant;
3. builds and starts Apache Jena Fuseki;
4. configures persistent TDB2-backed `/world` and `/char` datasets;
5. waits for Fuseki readiness;
6. runs a one-shot `fuseki-init` service that replaces the three canonical AIOS ontology graphs with the versions in the checked-out repository.

The canonical graphs loaded into `/world` are:

```text
urn:aios:ontology:world
urn:aios:ontology:contentkind
urn:aios:ontology:world-asserted
```

The bootstrap is idempotent. Running `docker compose up -d --build` again refreshes those canonical ontology graphs but does not delete other world graphs, character graphs, PostgreSQL data, or Qdrant collections.

### Persistent storage

Compose creates named volumes:

```text
aios_postgres
aios_qdrant
aios_fuseki
```

`docker compose down` removes the containers and network but keeps those volumes.

Do not run the following unless you intentionally want to erase the container-managed AIOS databases:

```bash
docker compose down -v
```

### Default ports

The storage services bind to loopback by default:

```text
PostgreSQL: 127.0.0.1:5432
Fuseki:    127.0.0.1:3030
Qdrant:    127.0.0.1:6333 (HTTP)
Qdrant:    127.0.0.1:6334 (gRPC)
```

They are not exposed to other machines on the LAN by the default Compose configuration.

## Infrastructure configuration

The defaults are intentionally aligned with the current native AIOS application defaults, so a fresh local installation does not require environment variables.

Optional overrides are documented in `.env.example`.

To customize them:

```bash
cd ~/AIOS-workspace/aios_app
cp .env.example .env
```

Important values include:

```text
AIOS_DB_DSN
AIOS_FUSEKI_BASE_URL
AIOS_QDRANT_URL
AIOS_POSTGRES_DB
AIOS_POSTGRES_USER
AIOS_POSTGRES_PASSWORD
AIOS_POSTGRES_PORT
AIOS_FUSEKI_PORT
AIOS_QDRANT_HTTP_PORT
AIOS_QDRANT_GRPC_PORT
AIOS_JENA_VERSION
AIOS_FUSEKI_JAVA_OPTIONS
```

If you change the PostgreSQL database name, user, password, or host port, update `AIOS_DB_DSN` to match.

## Create the Python environment

From the parent directory of `aios_app`:

```bash
cd ~/AIOS-workspace
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r aios_app/requirements.txt
python -m spacy download en_core_web_sm
```

The spaCy model is a separate download. Installing the `spacy` Python package alone does not install `en_core_web_sm`.

## Launch AIOS

With the Compose infrastructure running, launch AIOS natively from the parent directory:

```bash
cd ~/AIOS-workspace
source .venv/bin/activate
python -m aios_app.launch
```

The launcher still owns PostgreSQL schema lifecycle. Before it starts the AIOS processes it runs:

```bash
python -m aios_app.migrate
python -m aios_app.db_check
```

The Docker PostgreSQL image creates the server and persistent database volume; it does not replace AIOS migrations.

The launcher then starts the native AIOS services in readiness-gated stages. Required core services include the accumulator, supervisor, pipeline runner, and API. The UI and Semantic Index are currently treated as optional services by the launcher.

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

## Verify the infrastructure

From the repository directory:

```bash
docker compose ps
```

PostgreSQL and Fuseki should report healthy. Qdrant should be running, and `fuseki-init` should have exited successfully after loading the ontology.

Useful checks:

```bash
# PostgreSQL container readiness
docker compose exec postgres pg_isready -U postgres -d postgres

# Qdrant
curl http://127.0.0.1:6333

# Fuseki world dataset
curl -G \
  --data-urlencode 'query=ASK {}' \
  http://127.0.0.1:3030/world/sparql

# Fuseki character dataset
curl -G \
  --data-urlencode 'query=ASK {}' \
  http://127.0.0.1:3030/char/sparql
```

To inspect the bootstrap output:

```bash
docker compose logs fuseki-init
```

## Verify the AIOS API

With `python -m aios_app.launch` running:

```bash
curl http://127.0.0.1:8000/healthz
```

Expected response:

```json
{"ok":true}
```

Default native endpoints are:

```text
API:    http://127.0.0.1:8000
Web UI: http://127.0.0.1:7860
```

When accessing AIOS from another device on the LAN, replace `127.0.0.1` for the AIOS API/UI with the host's LAN address and ensure the relevant firewall rules allow those application ports. The database ports remain localhost-only unless the Compose file is deliberately changed.

## Day-to-day commands

Start or reconcile the infrastructure:

```bash
cd ~/AIOS-workspace/aios_app
docker compose up -d --build
```

View status:

```bash
docker compose ps
```

Follow infrastructure logs:

```bash
docker compose logs -f
```

Stop the infrastructure while preserving data:

```bash
docker compose down
```

Restart one service:

```bash
docker compose restart qdrant
```

Re-run only the Fuseki ontology bootstrap:

```bash
docker compose run --rm fuseki-init
```

## Existing installations

The Compose stack uses new Docker named volumes by default. It does not automatically import an existing PostgreSQL cluster, Qdrant directory, or Fuseki database from a previous manual installation.

For an existing AIOS installation with valuable data, back up each store before switching storage ownership. Do not delete the old services or directories until the Compose-backed installation has been verified. Migration of existing large stores should be treated as a separate data-migration operation rather than hidden inside installation startup.

## Common startup problems

### Docker port already in use

If an older PostgreSQL, Fuseki, or Qdrant service is still running on the host, Compose may fail to bind the default port.

Check:

```bash
ss -ltnp | grep -E ':5432|:3030|:6333|:6334'
```

Either stop the old service or change the corresponding port in `.env`.

### `fuseki-init` exits non-zero

Inspect:

```bash
docker compose logs fuseki
docker compose logs fuseki-init
```

The initializer requires the `/world` dataset to be healthy and the ontology files under `rdf/ontology/` to be present.

### PostgreSQL readiness fails in AIOS

Check both layers:

```bash
docker compose ps postgres
docker compose logs postgres
```

Then confirm `AIOS_DB_DSN` matches the Compose PostgreSQL settings.

You can run the AIOS checks directly:

```bash
cd ~/AIOS-workspace
source .venv/bin/activate
python -m aios_app.migrate
python -m aios_app.db_check
```

### spaCy cannot find `en_core_web_sm`

Install the model inside the active virtual environment:

```bash
python -m spacy download en_core_web_sm
```

### `No module named aios_app`

Run `python -m aios_app.launch` from the parent directory of the checkout/package, not from inside the package directory, and make sure the checkout directory is named `aios_app` or otherwise available on `PYTHONPATH`.

## Architecture boundary

The intended ownership is:

```text
Docker Compose
├── PostgreSQL process + persistent files
├── Qdrant process + persistent files
├── Fuseki process + persistent TDB2 files
└── Fuseki ontology bootstrap

Native AIOS
├── PostgreSQL migrations
├── pipeline/runtime services
├── Semantic Index collection logic
├── API
├── UI
└── Python/NLP/model environment
```

This boundary intentionally keeps Docker out of the active AIOS Python development loop while making its network dependencies reproducible.

For the system design after startup, see [architecture.md](architecture.md).
