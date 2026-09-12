# Running AIOS Development

AIOS keeps its Python application native while Docker Compose manages PostgreSQL, Apache Jena Fuseki, and Qdrant.

## Requirements

Install these before running AIOS:

- Python 3.10+
- Docker with Docker Compose v2

The setup script checks both and stops with a direct error if either is unavailable.

## Repository/package layout

The repository root is also the `aios_app` Python package. For now, clone it into a directory named `aios_app`:

```text
workspace/
├── .venv/
└── aios_app/
    ├── compose.yaml
    ├── setup.sh
    ├── run.sh
    ├── stop.sh
    └── ...
```

## First-time setup

```bash
mkdir -p ~/AIOS-workspace
cd ~/AIOS-workspace

git clone --branch AIOS-development https://github.com/LeetHappyfeet/AIOS.git aios_app
cd aios_app
./setup.sh
```

`setup.sh` performs only the minimum first-time work:

1. verifies Python 3.10+;
2. verifies Docker, Docker Compose, and the Docker daemon;
3. starts the Compose infrastructure with `docker compose up -d --build`;
4. waits for PostgreSQL and Fuseki readiness;
5. waits for the one-shot Fuseki ontology bootstrap to complete successfully;
6. waits for Qdrant to respond;
7. creates `../.venv` if it does not already exist;
8. installs `requirements.txt`;
9. installs spaCy `en_core_web_sm` if it is not already present;
10. runs `python -m aios_app.migrate`;
11. runs `python -m aios_app.db_check`.

The script is safe to run again after a partial setup failure. Existing Docker volumes and the existing virtual environment are reused rather than deleted.

The installer does not install Docker, Python, or operating-system packages for you. It reports which prerequisite is missing so the host remains under the user's control.

## Default infrastructure

Compose manages:

- PostgreSQL;
- Apache Jena Fuseki;
- Qdrant;
- the one-shot Fuseki ontology bootstrap.

The default PostgreSQL connection intentionally remains:

```text
postgresql://postgres:postgres@127.0.0.1:5432/postgres
```

Default ports are loopback-only:

```text
PostgreSQL: 127.0.0.1:5432
Fuseki:    127.0.0.1:3030
Qdrant:    127.0.0.1:6333 (HTTP)
Qdrant:    127.0.0.1:6334 (gRPC)
```

Compose creates persistent named volumes:

```text
aios_postgres
aios_qdrant
aios_fuseki
```

The Fuseki bootstrap loads the canonical ontology graphs into `/world`:

```text
urn:aios:ontology:world
urn:aios:ontology:contentkind
urn:aios:ontology:world-asserted
```

It does not delete ordinary `/world` or `/char` data.

Optional infrastructure overrides are documented in `.env.example`. A normal fresh install does not need a `.env` file.

## Run AIOS

After setup:

```bash
cd ~/AIOS-workspace/aios_app
./run.sh
```

`run.sh`:

1. verifies the existing native virtual environment;
2. verifies Docker is reachable;
3. runs `docker compose up -d` so the storage services are available;
4. starts `python -m aios_app.launch` from the package parent directory using the virtual environment directly.

The AIOS launcher continues to own application startup, migrations, service supervision, and readiness gating.

A healthy launch ends with output similar to:

```text
✅ AIOS READY
   Required services: 4/4 ready
```

Default application endpoints:

```text
API:    http://127.0.0.1:8000
Web UI: http://127.0.0.1:7860
```

Press `Ctrl+C` to stop the native AIOS processes. PostgreSQL, Qdrant, and Fuseki remain running so AIOS can be started again quickly.

## Stop the infrastructure

When you also want the Compose services stopped:

```bash
cd ~/AIOS-workspace/aios_app
./stop.sh
```

This runs `docker compose down` and preserves the named volumes.

Do not run this unless you intentionally want to erase the container-managed databases:

```bash
docker compose down -v
```

## Manual infrastructure inspection

The wrapper scripts intentionally stay small. Standard Docker commands remain available when debugging:

```bash
docker compose ps
docker compose logs -f
docker compose logs fuseki-init
docker compose restart qdrant
```

Direct readiness checks:

```bash
# PostgreSQL
docker compose exec postgres pg_isready -U postgres -d postgres

# Qdrant
curl http://127.0.0.1:6333

# Fuseki world dataset
curl -G --data-urlencode 'query=ASK {}' http://127.0.0.1:3030/world/sparql

# Fuseki character dataset
curl -G --data-urlencode 'query=ASK {}' http://127.0.0.1:3030/char/sparql
```

## Existing installations

The first-time installer is for a fresh AIOS environment. It does not import an existing PostgreSQL cluster, Qdrant directory, or Fuseki database into the Compose named volumes.

For an installation with valuable existing data, migrate or restore those stores deliberately before switching storage ownership. Do not delete the previous databases until the Compose-backed copies have been verified.

## Common failures

### Docker daemon is not reachable

Start Docker, then run:

```bash
./setup.sh
```

or, after setup:

```bash
./run.sh
```

### A database port is already in use

An older PostgreSQL, Fuseki, or Qdrant process may already own one of the default ports.

```bash
ss -ltnp | grep -E ':5432|:3030|:6333|:6334'
```

Stop the older service or configure a different port through `.env`.

### Fuseki bootstrap fails

```bash
docker compose logs fuseki
docker compose logs fuseki-init
```

The initializer requires a healthy `/world` dataset and the ontology files under `rdf/ontology/`.

### Python venv creation fails

Install your operating system's Python venv package, then run `./setup.sh` again. The script will reuse the already-created infrastructure.

### `No module named aios_app`

The repository currently needs to be named `aios_app`, with the virtual environment and Python invocation located in its parent directory. The provided setup and run scripts enforce this layout automatically.

## Architecture boundary

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

The scripts are orchestration only. They do not duplicate migrations, ontology definitions, or runtime logic.

For the system design after startup, see [architecture.md](architecture.md).
