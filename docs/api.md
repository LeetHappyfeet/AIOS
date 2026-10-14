# API guide

AIOS exposes an HTTP JSON API. Start with the [integration tutorial](integration.md)
for a complete interaction. Default base URL: `http://127.0.0.1:8000`.

## Discover the running contract

The canonical `aios_app.main:app` uses FastAPI's default documentation routes:

- [Swagger UI](http://127.0.0.1:8000/docs): schemas and interactive requests.
- [ReDoc](http://127.0.0.1:8000/redoc): browsable reference.
- [OpenAPI JSON](http://127.0.0.1:8000/openapi.json): machine-readable contract.

These links refer to the machine running your browser. Replace the host when
AIOS runs elsewhere. Interactive mutation requests write real data.

Export the schema from your running release, including routes installed by
`agent/api.py` and the canonical ingestion override:

```bash
curl --fail-with-body -sS http://127.0.0.1:8000/openapi.json -o aios-openapi.json
```

Some routes return dictionaries without full response models, so OpenAPI alone
does not describe every response field. The sections below document the main
client boundary. The app's current OpenAPI version string is `0.3.0`; record the
Git commit as well, since this string does not identify the full development
contract or effective database policy.

Use `Content-Type: application/json` for JSON bodies. HUD options are query
parameters, not JSON fields. This API currently has no common authentication
scheme installed in its entrypoint and permits broad CORS. For a same-machine
installation, bind the API to loopback with `AIOS_API_HOST=127.0.0.1`. Remote
access needs an explicitly configured access boundary; CORS is not authentication.

## POST /session

| Request field | Required | Meaning |
|---|---|---|
| `source` | No | Application name; explicitly set it for reliable resume |
| `source_session_id` | No | Stable external conversation key |
| `topic` | No | Display topic; defaults to empty string |
| `meta` | No | JSON object; defaults to `{}` |

Providing the same source and nonempty source session key resolves the existing
session. Without `source_session_id`, each call creates a new session. Response:

```json
{"session_id":"11111111-1111-4111-8111-111111111111","topic":"First integration"}
```

UUIDs in this guide are illustrative. Use returned IDs in real requests.

## POST /character/{character_id}/activate

The identity must exist first. The tutorial uses
`POST /character/{character_id}/identity/bootstrap/card` with a name-only card.
A full authored card can alter accepted identity: review `auto_accept_authored`
and `replace_authored_facets`, which both default to true.

| Request field | Required | Default / purpose |
|---|---|---|
| `user_name` | Yes | Stable human actor key |
| `session_id` | No | Supply the session UUID for live conversation |
| `scope_key` | No | `default`; tutorial explicitly uses `conversation` to match ingestion |
| `world_id`, `world_key` | No | Explicit world selection; omit both for the character's session branch |
| `controller_type` | No | `agent`; also `human`, `system`, `tool`, `script` |
| `controller_ref` | No | Controller identifier |

Response fields: `character_id`, `instance_id`, `entity_id`, `world_id`,
`timeline_id`, nullable `head_node_id`, integer `state_version`, and
`lifecycle_state`. Activation materializes or resumes runtime state; it is not a
read-only lookup. Pass the first participant's returned `world_id` when
activating another participant in that world.

## POST /ingest

| Request field | Required | Meaning |
|---|---|---|
| `session_id` | Yes | Session UUID |
| `character_id` | Yes | Conversation's target character identity |
| `user_name` | Yes | Stable human actor key |
| `text` | Yes | Observed message text |
| `speaker_type` | No | `user` by default; also `character`, `agent`, `system`, `tool`, `source` |
| `speaker_id`, `recipient_id` | No | Explicit actor IDs; set them in live clients |
| `viewpoint_id` | No | Explicit first-person identity override; use only when intended |
| `kind` | No | `chat_message` by default; see OpenAPI enum for other kinds |
| `scope_key` | No | Uses configured default; explicitly match activation |
| `payload` | No | Source metadata object; set `source` to your application name |
| `dedupe_key` | No | Explicit logical message revision key; strongly recommended |

`character_id` and `user_name` must differ. User and character speaker IDs cannot
collide with the opposite actor's key. Observations from external documents or
sensors have a separate `/observation` contract; see its schema before using it.

Illustrative response:

```json
{
  "ok": true,
  "event_id": 42,
  "node_id": "22222222-2222-4222-8222-222222222222",
  "timeline_id": "33333333-3333-4333-8333-333333333333",
  "disposition": "new",
  "source_head_node_id": "22222222-2222-4222-8222-222222222222",
  "source_current": true
}
```

`disposition` is `new`, `active_replay`, or `superseded_reselection`. The latter
belongs to source replacement/reselection handling; it is not a generic edit
operation. `source_head_node_id` can be null. `source_current` tells you whether
this result is at the active head. Successful ingestion does not promise that
claims, beliefs, topology, or goals have completed processing.

## POST /instance/{instance_id}/hud

| Query parameter | Default | Meaning |
|---|---|---|
| `through_node_id` | Current source head | Explicit source UUID returned by ingestion; use for live turns |
| `wait_ms` | `2500` | Readiness wait budget; runtime clamps to 0–10000 ms |
| `recent_limit` | Profile/runtime default | Recent-event limit |
| `token_budget` | Profile/runtime default | HUD assembly token budget |

The wait budget is not an end-to-end HTTP deadline; assembly and database work
also take time. Set the client timeout separately.

The response contains `instance_id`, `generation_ready`, `freshness`, `frame`,
and `text`. `frame` is structured state; `text` is the canonical rendered HUD.
Both come from the same preparation request.

Check `generation_ready` before generation. An HTTP 200 may still contain false.
Inspect `freshness.requested_source_node_id`, `source_head_node_id`,
`source_current`, `runtime_current`, and `topology_current` when present. A ready
HUD does not imply every background topology/enrichment task has completed.

A noncurrent source coordinate usually produces 409. The runtime also supports
replaying a matching cached snapshot and marks it with `replayed_snapshot` and
`active_source_head_node_id`. A new live turn should not silently use a replayed
historical snapshot. The example client rejects it.

## Errors and retry decisions

| Result | Client action |
|---|---|
| 200 with `generation_ready:false` | Bounded HUD retry at the same coordinate; inspect runtime if it remains false |
| 409 from HUD | Read instance state and reconcile the requested source head; do not retry indefinitely |
| 404 from activation/HUD/state | Check identity, instance, or node existence and IDs |
| 422 | Fix request validation or actor-key collision; retrying unchanged input will not help |
| 400/500 from ingestion | Preserve the error and message key; investigate storage/DAG failure before replay |
| Transport timeout | Outcome may be unknown; retry an ingest only with its original key and content |
| 503 from runtime versions | Database introspection is unavailable |

FastAPI errors normally use `{"detail": ...}`. Validation details can be an
array; not every route currently has a uniform error schema.

## Additional API surfaces

This is a navigation index, not a stability guarantee for experimental routes.
Use the running OpenAPI schema for complete fields and additional operations.

| Purpose | Representative routes | Status / audience |
|---|---|---|
| State and actions | `GET /instance/{instance_id}/state`, `POST /instance/{instance_id}/action` | Runtime integration |
| Entities and branching | `POST /world/{world_id}/entity`, `POST /instance/{instance_id}/fork` | Runtime integration |
| Identity | `GET /character/{character_id}/identity`, identity source and candidate routes | Explicit identity management |
| Epistemic search | `POST /epistemic/search`, `GET /epistemic/proposition/{proposition_id}` | Knowledge inspection |
| Corpus | `POST /corpus/document`, `POST /instance/{instance_id}/corpus/consume` | Experimental acquisition |
| Agent tasks | `POST /agent/instance/{instance_id}/task`, wake and heartbeat routes | Experimental cognition |
| Inference | `GET/POST /inference/providers`, provider health/control routes | Operator / experimental |
| Diagnostics | `POST /semantic/inspect`, `GET /semantic/clusters`, `GET /agent/runtime/versions` | Operator |
| Compatibility | `/memory`, `/instance/{instance_id}/prepare`, `/frame`, `/frame/text` | Deprecated; use `/hud` for live generation |

Public routes currently have no common `/v1` prefix. Pin a release/commit,
retain its OpenAPI export, and test the integration when upgrading.
