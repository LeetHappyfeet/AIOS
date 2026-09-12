#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

command -v docker >/dev/null 2>&1 || {
    printf 'ERROR: Docker is not installed or not on PATH.\n' >&2
    exit 1
}

docker info >/dev/null 2>&1 || {
    printf 'ERROR: The Docker daemon is not reachable.\n' >&2
    exit 1
}

cd "$REPO_DIR"
docker compose down
