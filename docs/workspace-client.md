# AIOS Client workspace support (protocol 1)

The separately installed AIOS_Client application hosts model inference and workspace
operations. This API binds one source-writing workspace to an existing character
instance. It does not create characters, change continuity, manage SSH, or mount files.

Install this revision through the normal AIOS upgrade/migration procedure. Export
`AIOS_WORKSPACE_API_KEY` before starting AIOS. Workspace routes require
`Authorization: Bearer <key>`; they return 503 until that key is configured. This is a
shared operator credential, not per-character authentication. Use a private connection
or HTTPS. Existing AIOS endpoints retain their existing authentication behavior.

The local client setup page stores AIOS host, port, protocol and base path; a model
base URL, model ID and optional credential reference; and agent profiles. Agent
username identifies a client worker, not a new AIOS character. The character, instance,
session, human routing key and scope must match an existing active runtime. The
runtime and source timelines may differ; both must describe the same session actors.
An initial source observation and a generation-ready HUD are required. A dedicated
workspace conversation should be prepared using the existing activation/ingestion API.

All following paths are below `/workspace-client`:

| Method and path | Purpose |
|---|---|
| GET `/protocol` | Version and supported capabilities |
| GET `/instances?character_id=...` | List existing session coordinates for explicit selection |
| POST `/bind` | Validate and persist a workspace/worker/instance binding |
| POST `/{binding_id}/context` | Exact-source current HUD, using canonical HUD preparation |
| POST `/{binding_id}/submit` | Explicit operator assignment: request_id UUID and objective |
| POST `/{binding_id}/claim` | Claim next assignment with worker_key |
| POST `/{binding_id}/runs/{run_id}/renew` | Renew with worker_key and claim_token |
| POST `/{binding_id}/runs/{run_id}/observe` | Bounded tool observation with event_key UUID, text and payload |
| POST `/{binding_id}/runs/{run_id}/finish` | Terminal worker report with status and result |
| GET `/{binding_id}/runs/{run_id}` | Assignment status |

Submit creates a `workspace.run` character action, approves only the explicitly
submitted operator assignment, and dispatches through existing action policy. A deny
policy still prevents execution. First-pass submissions are operator initiated;
registering the action alone does not make internal planners generate workspace jobs.

The dedicated claim path scopes work to the persisted binding. Generic remote worker
claims exclude workspace.run. A 120-second lease is renewed every 20 seconds by the
client. Expired work is marked interrupted, never requeued automatically: an unknown
shell side effect cannot safely be repeated. Every observation and finish requires
its current claim token. Finish retries with identical content are idempotent.

Observations use the existing `ingest_message` path, with speaker_type=tool,
kind=tool_result, an explicit workspace speaker, and the character as recipient.
Runtime source adoption and existing cognition admission occur through that path.
They are not character speech or automatically accepted world facts. The receipt,
source advance, and workspace cursor update share one database transaction. A
repeated event_key with changed content is rejected.

The runtime source head must still equal the workspace's last observed coordinate.
If another client advances it, the next context/renewal/observation returns 409 and
the client stops. Simultaneous chat/coding writes to one instance are not supported
in this pass. Multiple independent instances can have separate bindings. Changing a
binding or recovering from a source conflict requires operator reconciliation; no
automatic cursor reset or instance substitution is provided.

Full logs stay on the client. Artifact identifiers refer to that client's retained
files, not server-local paths or downloadable URLs. AIOS receives bounded results and
worker reports. A succeeded workspace run does not itself verify a character goal.

Tests: `tests/test_workspace_client.py` runs SQL contracts in an isolated disposable
PostgreSQL database named *_workspace_test via AIOS_WORKSPACE_TEST_DSN. The fixture
replaces its aios schema; never point it at a real installation. The test migration
uses minimal referenced tables; the ordinary migrator remains responsible for full
installation upgrades. The workflow workspace-client.yml supplies PostgreSQL 16.
