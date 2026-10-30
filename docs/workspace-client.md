# AIOS Client workspace support (protocol 2)

The separately installed AIOS_Client application hosts model inference and workspace
operations. This API binds one source-writing workspace to an existing character
instance. It does not create characters, change continuity, manage SSH, or mount files.

Install this revision through the normal AIOS upgrade/migration procedure. Open Gradio → **Workspace Access**, generate or enter a key, and click
**Save workspace key**. Use **Reveal active key for copying** to copy it into the
AIOS Client AIOS connection settings. This is separate from the model provider key.
AIOS saves it in `launch-config.json` beside `launch.py`, with owner-only Unix
permissions and atomic replacement. It survives restarts and applies to the API
without restarting. The key is plaintext; keep this file private and Gradio limited
to trusted operators, who can reveal or replace it.

For a different location, set `AIOS_LAUNCH_CONFIG` to an absolute file path in an
existing writable directory before launching AIOS. An explicitly set
`AIOS_WORKSPACE_API_KEY` (including an empty value) takes precedence over the saved
key; remove that setting and restart to use the Gradio-managed key. Workspace routes require
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

## Concurrent character environments (first pass, protocol v3 capabilities)

Apply additive migration `20261028_02_character_environments.sql` after the v2
migrations and restart all AIOS processes. Existing v2 bindings and assignments
continue to work. `/protocol` retains `version: 2` for existing clients and adds
`supported_versions: [2, 3]`, `environment_protocol: 3`, and named v3 capabilities.
A worker must send `protocol_version: 3` in its ordinary claim request to receive
new delegated runs. A v2 claim can never take a v3 run. The assignment schema and
hash algorithm remain v2-compatible; delegation coordinates live on the run.

Prepare **two already active instances**, with the same `character_id` and runtime
human user, distinct source timelines, and independent runtime worlds. Keep the
workspace's existing session/user/scope binding. Merely changing `payload.source`
does not create a separate source timeline. Do not activate the second environment
in the chat's world: activation can reuse the same character/world/user instance.
This first pass validates existing instances; it does not automatically provision
or select one, reset source cursors, or merge worlds.

Link management, delegation, cancellation and review require the distinct
`AIOS_WORKSPACE_REVIEW_KEY`. Workers retain only `AIOS_WORKSPACE_API_KEY`.
Paths below are relative to `/workspace-client`:

| Method and path | Credential | Body / purpose |
|---|---|---|
| POST `/v3/{binding_id}/links` | Operator | `origin_instance_id`, `share_memory` (default false) |
| POST `/v3/{binding_id}/links/{link_id}/revoke` | Operator | Revoke sharing and request cancellation of outstanding linked runs |
| POST `/v3/{binding_id}/links/{link_id}/submit` | Operator | Ordinary assignment fields and request ID, plus the exact current origin `source_node_id` |
| POST `/v3/{binding_id}/runs/{run_id}/cancel` | Operator | Cancel queued work or request cooperative stopping of running work |
| POST `/v3/{binding_id}/runs/{run_id}/context` | Worker | `worker_key`, `claim_token`; task-focused execution HUD |
| POST `/v3/{binding_id}/runs/{run_id}/recall` | Worker | `worker_key`, `claim_token`, `query` (up to 600 characters) |
| POST `/v3/{binding_id}/runs/{run_id}/progress` | Worker | Lease identity, UUID `event_key`, `kind` (`progress` or `blocked`), bounded `summary` |
| GET `/v3/{binding_id}/runs/{run_id}/events?after=0` | Worker | Up to 100 ordered events, with `next_cursor` for reconnect/paging |

Keep the normal `/renew`, `/observe`, `/finish` and `/review` paths. `/renew`
now also returns `cancel_requested`. New clients must check this before each next
tool/model step and stop owned commands when requested. Revocation immediately
blocks recall, context and observations; a live lease can still renew to receive
the stop request and report its cancelled finish. A finish racing with a cancellation
is recorded as cancelled, never accepted as goal completion. Commands already
started may have effects; cancellation does not roll back files. Unknown or expired
execution is never replayed automatically.

### Cognitive continuity

`share_memory: true` adds only the directly linked peer to
`aios.cognitive_evidence_instances`. A-B plus B-C does not grant A access to C.
Character identity, runtime user and existing `isolated` policy remain enforced.
An isolated policy must be changed explicitly before sharing. Revoked links cannot
be silently re-enabled; this pass requires a newly prepared binding/explicit
reconciliation rather than reusing a revoked association.

The same scope is used by HUD retrieval and the existing character-accessible
Inquiry lookup. Acquisitions, semantic admission, authorized uses, source
supersession and attribution checks still apply. No memories are copied and no
raw alternate transcript becomes globally readable. Work observations retain the
`aios-client` source and their original tool role. A recall hit includes provenance
and its durable-knowledge flag; a reference hit is not acquired knowledge.
`unavailable` and scope changes are distinct from finding no matches.

The execution context uses the named `workspace_execution` HUD profile and the
immutable assignment objective as retrieval focus. The client still supplies the
full assignment/constraints separately. Add an AIOS recall tool to its model tool
registry so the model can ask what it tried previously **during** execution;
fetching a startup HUD alone does not provide this behavior. The server derives
recall identity from the binding/run, not a model-provided character or instance ID.
Knowledge/admission revisions and scope membership invalidate prepared retrieval
and HUD snapshots without advancing the chat source. Explicitly profiled HUDs
are rebuilt to avoid reusing another environment's presentation.

### Delegation, task delivery and goals

A run persists origin instance/timeline/node and optional origin goal/attempt,
while its action executes on the workspace instance. The original goal is never
cloned onto the executor. Ordinary later chat turns do not invalidate its lease.
A changed origin timeline or a source node no longer on the active DAG ancestry
stops further work. Identical submission retries resolve the existing run even
after later chat turns. Changed assignment/link/source with the same request ID
returns 409. To amend work in this pass, cancel and submit a new assignment with a
new request ID; there is no in-place change to immutable criteria.

Queued, claimed, progress/blocked, terminal, cancellation and review events are
stored transactionally with the lifecycle change. Event keys are idempotent and
changed-content retries are rejected. Progress summaries are worker reports, not
verification. Completion wakes both executor and origin, with a shared evidence
reference. Review still requires passing evidence and the original live attempt;
late evidence cannot close a newer attempt. Revocation blocks acceptance.

The authoritative `/instance/{instance_id}/hud` response adds `frame.remote_tasks`
and `frame.hud.task_event_watermark`. The renderer includes a bounded REMOTE WORK
section for the linked originating instance and source timeline, even when the
underlying cognitive HUD was cached. It does not insert tool reports as chat
messages or wait for semantic enrichment. The overlay is read at request time;
SillyTavern must request a new HUD or poll run events to display later updates.
An already-running model generation retains the context it received. There is no
server push transport or automatic generation interruption in this pass.

The existing AIOS_Client v2 runner has not been modified here. Its companion
update needs protocol negotiation, v3 claims/context, a recall tool, structured
progress, cancellation handling, and persisted event cursors. SillyTavern can
consume the rendered section on its next normal HUD request; immediate status UI
requires integration-side polling. Purpose-based character requests now enter AIOS work admission; see
[work admission](work-admission.md) for profiles, policy, source markup and native
request transport. Direct link submissions remain explicit operator actions.
Link/goal access is never inferred from character names.

Focused validation: `tests/test_character_environments.py`,
`tests/test_environment_hud_context.py`, and `tests/test_workspace_client.py`.
The workspace fixtures support PostgreSQL or isolated `AIOS_PGLITE_MODULE`, reuse
the real migration and lifecycle SQL, and stub the independent scene projector.
Recall tests verify identity/lease/revocation boundaries using a stub retrieval
backend; they do not claim live model or production recall validation.
