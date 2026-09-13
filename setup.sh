#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_DIR="$(dirname "$REPO_DIR")"
VENV_DIR="$WORKSPACE_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"

ok() {
    printf '[✓] %s\n' "$1"
}

fail() {
    printf '\nERROR: %s\n' "$1" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || fail "$2"
}

printf '\nAIOS first-time setup\n\n'

[ -f "$REPO_DIR/__init__.py" ] || fail "This does not look like the AIOS application repository: missing __init__.py"
[ -f "$REPO_DIR/aios_baseline.sql" ] || fail "Missing canonical database baseline: aios_baseline.sql"
[ -d "$REPO_DIR/migrations/current" ] || fail "Missing active migrations directory: migrations/current"
[ ! -e "$REPO_DIR/aios_schema.sql" ] || fail "Obsolete aios_schema.sql is present. Update the checkout before running first-time setup."

require_command python3 "Python 3 is required. Install Python 3.10 or newer and run 'bash setup.sh' again."
require_command docker "Docker is required. Install Docker with the Compose plugin and run 'bash setup.sh' again."

docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required ('docker compose')."
docker info >/dev/null 2>&1 || fail "Docker is installed, but the Docker daemon is not reachable. Start Docker and run 'bash setup.sh' again."

PYTHON_VERSION="$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
    || fail "Python 3.10 or newer is required. Found Python $PYTHON_VERSION."

ok "Python $PYTHON_VERSION"
ok "Docker"
ok "Docker Compose"
ok "Canonical database baseline"
ok "Post-baseline migration directory"

printf '\nStarting AIOS infrastructure...\n'
cd "$REPO_DIR"
docker compose up -d --build

POSTGRES_ID="$(docker compose ps -q postgres)"
FUSEKI_ID="$(docker compose ps -q fuseki)"
INIT_ID="$(docker compose ps -a -q fuseki-init)"

[ -n "$POSTGRES_ID" ] || fail "PostgreSQL container was not created."
[ -n "$FUSEKI_ID" ] || fail "Fuseki container was not created."
[ -n "$INIT_ID" ] || fail "Fuseki bootstrap container was not created."

wait_for_health() {
    container_id="$1"
    attempts=60

    while [ "$attempts" -gt 0 ]; do
        status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id" 2>/dev/null || true)"
        case "$status" in
            healthy|running)
                return 0
                ;;
            unhealthy|exited|dead)
                return 1
                ;;
        esac
        attempts=$((attempts - 1))
        sleep 2
    done
    return 1
}

wait_for_health "$POSTGRES_ID" || {
    docker compose logs --tail=80 postgres >&2 || true
    fail "PostgreSQL did not become healthy."
}
ok "PostgreSQL ready"

wait_for_health "$FUSEKI_ID" || {
    docker compose logs --tail=80 fuseki >&2 || true
    fail "Fuseki did not become healthy."
}
ok "Fuseki ready"

attempts=60
while [ "$attempts" -gt 0 ]; do
    init_status="$(docker inspect -f '{{.State.Status}}' "$INIT_ID" 2>/dev/null || true)"
    if [ "$init_status" = "exited" ]; then
        init_code="$(docker inspect -f '{{.State.ExitCode}}' "$INIT_ID")"
        if [ "$init_code" = "0" ]; then
            ok "Fuseki ontology ready"
            break
        fi
        docker compose logs --tail=120 fuseki-init >&2 || true
        fail "Fuseki ontology bootstrap failed."
    fi
    attempts=$((attempts - 1))
    sleep 2
done
[ "$attempts" -gt 0 ] || fail "Timed out waiting for the Fuseki ontology bootstrap."

attempts=60
while [ "$attempts" -gt 0 ]; do
    if python3 - <<'PY'
import urllib.request
try:
    with urllib.request.urlopen("http://127.0.0.1:6333/", timeout=2) as response:
        raise SystemExit(0 if 200 <= response.status < 500 else 1)
except Exception:
    raise SystemExit(1)
PY
    then
        ok "Qdrant ready"
        break
    fi
    attempts=$((attempts - 1))
    sleep 2
done
if [ "$attempts" -eq 0 ]; then
    docker compose logs --tail=80 qdrant >&2 || true
    fail "Qdrant is not responding on http://127.0.0.1:6333."
fi

printf '\nPreparing native Python environment...\n'
if [ ! -x "$VENV_PYTHON" ]; then
    python3 -m venv "$VENV_DIR" || fail "Could not create $VENV_DIR. Install the Python venv package for your operating system and retry."
    ok "Virtual environment created"
else
    ok "Virtual environment exists"
fi

# The repository itself is the Python package historically named `aios_app`.
# Make that package name stable regardless of what directory the user chose for
# the git clone. This avoids requiring clones to literally be named aios_app.
SITE_PACKAGES="$($VENV_PYTHON -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
PACKAGE_LINK="$SITE_PACKAGES/aios_app"
if [ -L "$PACKAGE_LINK" ]; then
    CURRENT_TARGET="$(readlink -f "$PACKAGE_LINK" || true)"
    if [ "$CURRENT_TARGET" != "$REPO_DIR" ]; then
        rm "$PACKAGE_LINK"
        ln -s "$REPO_DIR" "$PACKAGE_LINK"
    fi
elif [ -e "$PACKAGE_LINK" ]; then
    fail "Cannot register AIOS package alias because $PACKAGE_LINK already exists and is not a symlink."
else
    ln -s "$REPO_DIR" "$PACKAGE_LINK"
fi
ok "Python package alias aios_app -> $REPO_DIR"

"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PYTHON" -m pip install -r "$REPO_DIR/requirements.txt"
ok "Python requirements installed"

if "$VENV_PYTHON" -c 'import spacy; spacy.load("en_core_web_sm")' >/dev/null 2>&1; then
    ok "spaCy en_core_web_sm already installed"
else
    "$VENV_PYTHON" -m spacy download en_core_web_sm
    ok "spaCy en_core_web_sm installed"
fi

"$VENV_PYTHON" -c 'import aios_app; print(aios_app.__file__)' >/dev/null \
    || fail "AIOS Python package alias could not be imported."
ok "AIOS Python package import"

printf '\nInitializing AIOS PostgreSQL database...\n'
cd "$WORKSPACE_DIR"

printf 'Database target: '
"$VENV_PYTHON" -c 'from aios_app.config import settings; print(settings.db_dsn)'

if ! "$VENV_PYTHON" -m aios_app.migrate; then
    fail "AIOS database initialization failed. Fresh installs must use the canonical aios_baseline.sql plus migrations/current. If this Docker volume contains a prototype-era AIOS database, preserve it if needed and recreate the PostgreSQL volume before retrying."
fi
ok "Canonical baseline and migrations applied"

"$VENV_PYTHON" -m aios_app.db_check || fail "AIOS database readiness check failed."
ok "AIOS database ready"

# A first-time setup should also prove that the migration path is safe to run
# again, because every normal AIOS launch performs the same migration preflight.
"$VENV_PYTHON" -m aios_app.migrate || fail "AIOS migration idempotency check failed."
ok "Migration idempotency verified"

printf '\n========================================\n'
printf '        AIOS SETUP COMPLETE\n'
printf '========================================\n\n'
printf 'PostgreSQL was initialized from aios_baseline.sql and migrations/current/.\n'
printf 'Normal AIOS startup will re-check migrations automatically.\n\n'
printf 'Start AIOS with:\n\n'
printf '    cd %s\n' "$REPO_DIR"
printf '    bash run.sh\n\n'
printf 'API:    http://127.0.0.1:8000\n'
printf 'Web UI: http://127.0.0.1:7860\n\n'
