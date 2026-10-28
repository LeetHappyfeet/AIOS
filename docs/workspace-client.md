# AIOS Client workspace support (protocol 2)

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
| POST `/{binding_id}/submit` | Explicit operator assignment: request_id UUID plus structured assignment criteria |
| POST `/{binding_id}/claim` | Claim next assignment with worker_key |
| POST `/{binding_id}/runs/{run_id}/renew` | Renew with worker_key and claim_token |
| POST `/{binding_id}/runs/{run_id}/observe` | Bounded tool observation with event_key UUID, text and payload |
| POST `/{binding_id}/runs/{run_id}/finish` | Execution result plus structured worker evidence |
| GET `/{binding_id}/runs/{run_id}` | Assignment status |
| GET `/{binding_id}/runs/{run_id}/review` | Evidence bundle (separate review key) |
| POST `/{binding_id}/runs/{run_id}/review` | Accept/reject goal-linked evidence (separate review key) |

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
worker evidence. A `finished` run means execution ended. The underlying action may be
`succeeded` to record successful execution delivery; this is not goal success. Legacy
`succeeded` finish requests are normalized to `finished`. Required check failures become
`failed`. AIOS derives `result.verification_status` from the immutable assignment and
returned evidence rather than trusting a worker's `passed` label.

Tests: `tests/test_workspace_client.py` runs SQL contracts in an isolated disposable
PostgreSQL database named *_workspace_test via AIOS_WORKSPACE_TEST_DSN. The fixture
replaces its aios schema; never point it at a real installation. The test migration
uses minimal referenced tables; the ordinary migrator remains responsible for full
installation upgrades. The workflow workspace-client.yml supplies PostgreSQL 16.

## Upgrade and assignment contract

Apply `20261027_01_workspace_client.sql` followed by
`20261028_01_workspace_verification.sql` through the normal migrator. Server and client
must both speak v2. Existing v1 queued assignments have no criteria/hash: cancel and
resubmit them with new request IDs rather than treating old prose as verified work.
The second migration does not relabel historical run outcomes.

Submit body example (all fields except request_id/objective have defaults):

```json
{
  "request_id": "00000000-0000-4000-8000-000000000001",
  "objective": "Fix addition without changing the tests",
  "required_checks": [{"id": "unit", "command": "python -m unittest -v", "cwd": ".", "timeout_seconds": 120}],
  "expected_outputs": [{"path": "calc.py", "kind": "file"}],
  "preserve_paths": ["test_calc.py"],
  "budgets": {"model_steps": 60, "wall_seconds": 1800, "context_chars": 100000}
}
```

Optional goal_id/attempt_id UUIDs must appear together. Submission locks and validates
that the goal attempt is open and belongs to this instance/source timeline. The full
normalized assignment and SHA-256 are stored on workspace_run. A database trigger
prevents later criteria changes. Same request ID plus changed criteria returns 409.
Claim returns assignment and assignment_hash for client verification.

The client runs each required check after the model finishes. Evidence includes each
command/cwd/timeout, exit status, drain/cancellation/timeout state, retained log hash,
manifest hash, output expectations, preserved-path violations, and stale-snapshot
state. No required checks => not_configured. Missing/inconsistent evidence => incomplete.
Server validation establishes completeness, **not remote attestation**. It cannot
independently inspect files or validate the honesty/adequacy of the remote worker's
checks. Artifacts remain on the client. See the client README for exact snapshot scope
and limits; generated/dependency directories are normally excluded. A result proves
nothing about edits made after its final snapshot.

## Human review and goal outcomes

Set a distinct `AIOS_WORKSPACE_REVIEW_KEY` only in the AIOS server environment. Do not
share it with the workspace client, its model, or command environment. Worker-key
requests cannot use the review routes. The review key can GET the review bundle and
POST a decision without the worker key. If unset or identical to the worker key,
review is disabled (503).

```bash
curl -H "Authorization: Bearer $AIOS_WORKSPACE_REVIEW_KEY" \
  "$AIOS_URL/workspace-client/$BINDING_ID/runs/$RUN_ID/review"
curl -X POST -H "Authorization: Bearer $AIOS_WORKSPACE_REVIEW_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"accept":true,"reviewer":"operator","reason":"Inspected changes and retained check logs"}' \
  "$AIOS_URL/workspace-client/$BINDING_ID/runs/$RUN_ID/review"
```

Every goal-linked finish creates one pending workspace_review row and a runtime wake.
It never directly finishes a goal or mints a canonical deterministic-tool acquisition.
A human should inspect changes/logs and task adequacy before accepting. Acceptance
requires a finished run, structurally complete passing checks, current workspace source,
and the original still-open goal attempt. It calls CharacterGoalService.finish with
`verification=reviewed` and the workspace evidence reference, preserving the existing
outcome lifecycle. Closed/foreign/delayed attempts cannot close a newer attempt.
Identical review retries are idempotent; a different second decision returns 409.
Rejection records the reason and leaves the goal active for explicit reassignment or
other lifecycle decisions. There is no automatic retry, reward, or failure appraisal
from worker prose. Review is currently an API operation, not a new AIOS UI.
