#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_DIR="$(dirname "$REPO_DIR")"
VENV_PYTHON="$WORKSPACE_DIR/.venv/bin/python"

fail() {
    printf '\nERROR: %s\n' "$1" >&2
    exit 1
}

[ -x "$VENV_PYTHON" ] \
    || fail "AIOS is not set up yet. Run ./setup.sh first."

"$VENV_PYTHON" -c 'import aios_app' >/dev/null 2>&1 \
    || fail "AIOS Python package alias is missing or invalid. Run ./setup.sh again."

command -v docker >/dev/null 2>&1 \
    || fail "Docker is required to run the AIOS infrastructure."

docker info >/dev/null 2>&1 \
    || fail "The Docker daemon is not reachable. Start Docker and try again."

cd "$REPO_DIR"
docker compose up -d

cd "$WORKSPACE_DIR"
exec "$VENV_PYTHON" -m aios_app.launch "$@"
