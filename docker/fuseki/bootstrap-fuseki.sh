#!/bin/sh
set -eu

FUSEKI_BASE_URL="${FUSEKI_BASE_URL:-http://fuseki:3030}"
WORLD_DATA_URL="${FUSEKI_BASE_URL%/}/world/data"
WORLD_QUERY_URL="${FUSEKI_BASE_URL%/}/world/sparql"

wait_for_world() {
    attempt=1
    while [ "$attempt" -le 60 ]; do
        if curl --fail --silent --show-error --get \
            --data-urlencode 'query=ASK {}' \
            "$WORLD_QUERY_URL" >/dev/null 2>&1; then
            return 0
        fi
        echo "Fuseki bootstrap: waiting for /world (${attempt}/60)"
        attempt=$((attempt + 1))
        sleep 1
    done

    echo "Fuseki bootstrap: /world did not become ready" >&2
    return 1
}

replace_graph() {
    file="$1"
    graph="$2"

    if [ ! -f "$file" ]; then
        echo "Fuseki bootstrap: missing ontology file $file" >&2
        return 1
    fi

    echo "Fuseki bootstrap: loading $graph from $(basename "$file")"
    curl --fail --silent --show-error \
        --retry 5 \
        --retry-connrefused \
        --request PUT \
        --header 'Content-Type: text/turtle' \
        --data-binary "@$file" \
        "${WORLD_DATA_URL}?graph=${graph}" \
        >/dev/null
}

verify_graph() {
    graph="$1"
    result="$(curl --fail --silent --show-error --get \
        --header 'Accept: application/sparql-results+json' \
        --data-urlencode "query=ASK { GRAPH <${graph}> { ?s ?p ?o } }" \
        "$WORLD_QUERY_URL")"

    case "$result" in
        *'"boolean" : true'*|*'"boolean":true'*|*'"boolean" :true'*|*'"boolean": true'*)
            return 0
            ;;
        *)
            echo "Fuseki bootstrap: verification failed for $graph" >&2
            echo "$result" >&2
            return 1
            ;;
    esac
}

wait_for_world

replace_graph /ontology/world.ttl urn:aios:ontology:world
replace_graph /ontology/world-contentkind.ttl urn:aios:ontology:contentkind
replace_graph /ontology/world-asserted.ttl urn:aios:ontology:world-asserted

verify_graph urn:aios:ontology:world
verify_graph urn:aios:ontology:contentkind
verify_graph urn:aios:ontology:world-asserted

echo "Fuseki bootstrap: canonical AIOS ontology graphs are ready"
