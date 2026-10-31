# Work requests by capability and purpose

AIOS advertises authorized work capabilities in the current character's HUD. The
character proposes intent; AIOS selects a linked workspace, applies action policy,
and queues an immutable assignment. The client owns its local model/tool loop.
There is no LLM call just to translate an already structured request, and no wait
for the whole project inside conversational generation. Admission is not execution
or goal completion. Results continue through the existing REMOTE WORK overlay.

Apply `20261029_01_work_admission.sql` through the normal migrator after the existing
workspace/environment migrations and restart AIOS. This change works with the v3
client listener already on `development/v3-worker`; it does not require a new client
protocol. Standalone v2 clients cannot claim delegated v3 runs.

## Configure work AIOS may route

Attach the existing client binding to its conversational instance using Gradio
**Workspace Delegation**, or use an independent execution environment, as described
in [workspace-client.md](workspace-client.md#operator-setup-for-chat-delegation-gradio).
Attachment requires `20261030_02_workspace_attachment.sql` and keeps run evidence
separate from the chat cursor. Independent environments still require matching
character/user and separate source timelines/worlds.
No instance, machine, environment link, or permission is inferred from a name.

For open-ended experiments, Gradio's **Enable general experimental tasks on
selected link** creates `workspace / experimental_task`. Change `objective` for
each task; no per-task purpose name is needed. Its 100,000-character context cap
replaces the overly small greeting-test budget for new work. It verifies a
request-specific outcome/evidence report; it does not universally verify task
success. Existing approval and execution policies remain in force.

Operator template strings may contain `{request_id}`. Only AIOS's UUID is
substituted when creating the immutable assignment; objectives never supply shell
check text. Pending/approved profile snapshots keep the original operator template.

From the operator environment, configure a capability profile on the link:

```bash
curl -X POST -H "Authorization: Bearer $AIOS_WORKSPACE_REVIEW_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "capability":"software_development",
    "purpose":"inspect_and_repair",
    "description":"Inspect and repair the authorized project; run its tests",
    "assignment_template":{
      "required_checks":[{"id":"unit","command":"python -m unittest -v"}],
      "preserve_paths":["tests.py"],
      "expected_outputs":[{"path":"calc.py","kind":"file"}],
      "budgets":{"model_steps":60,"wall_seconds":1800,"context_chars":100000}
    }
  }' \
  "$AIOS_URL/workspace-client/v3/$BINDING_ID/links/$LINK_ID/work-profiles"
```

This example assumes a calculator workspace; configure actual project checks and
paths. At least one host check is required. Model requests cannot supply commands,
checks, goal/attempt IDs, credentials, link IDs, or destinations. Profile criteria
are immutable; use a new purpose or link for changed criteria. Reposting the same
criteria can change description/enabled state. Disable a profile with the same
body and `enabled:false`; revoking a link also stops its existing delegated runs.
Disabling a profile blocks future admission, not already queued work; use existing
run cancellation for that. Shell execution still has the client's account permissions;
these profiles do not add OS isolation or elevation.

Existing `workspace.run` action policies apply on **both** origin and execution
instances. `deny` prevents routing and hides the capability in the HUD. The default
external-sensitive policy is `require_approval`, so character requests wait. Set
explicit `allow` policies through normal operator policy management only when
unattended execution is appropriate for that configured environment. AIOS does not
convert a model request into an operator approval.

For a pending request, inspect the exact intent, selected binding, pinned checks and
goal coordinates before deciding. Both endpoints require the separate operator credential:

```bash
curl -H "Authorization: Bearer $AIOS_WORKSPACE_REVIEW_KEY" \
  "$AIOS_URL/workspace-client/work/$ORIGIN_INSTANCE_ID/$REQUEST_ID"
curl -X POST -H "Authorization: Bearer $AIOS_WORKSPACE_REVIEW_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"accept":true,"reviewer":"operator","reason":"Approved this bounded task and its pinned checks"}' \
  "$AIOS_URL/workspace-client/work/$ORIGIN_INSTANCE_ID/$REQUEST_ID/decision"
```

A request must have pinned profile criteria before it can be approved. Repeated
identical decisions return the existing receipt; conflicting decisions return 409.
A policy deny cannot be overridden here. Approval of execution does not approve
final goal evidence; the existing separate goal review remains required.

## HUD and the conversational request

Each HUD request reads capabilities and task status from current database records,
including when its cognitive frame was cached. The HUD exposes purpose, description,
and whether an authenticated v3 worker has polled/renewed within 45 seconds. This
is recent presence, not proof that a compiler is installed or that execution will
succeed. The operator profile defines supported purposes; the worker cannot grant
itself capabilities. No inbound VM connection is required.

For the current text-based integration, the character emits exactly one actual
control block outside Markdown code fences, inline code, or blockquotes. Quoted
examples in those forms are not dispatched:

```xml
<aios_action>{"op":"request_work","capability":"software_development","purpose":"inspect_and_repair","objective":"Find and fix incorrect calculator sums","constraints":["Retain the existing public interface"]}</aios_action>
```

AIOS handles this after ordinary ingestion adopts the source. It validates the
actual current character speaker, viewpoint, source timeline and node; human/tool
messages cannot trigger it. The block expresses a request, not a world fact. It is
stripped from episodic HUD display through the existing tool-markup renderer.
Historical/replayed ingestion does not reissue work. Malformed source requests
produce a needs-clarification receipt visible in the next HUD, without launching
commands. The original source remains available for audit.

The plugin only needs to ingest the character reply normally. Older integrations
also relay every action block to the research tool-request endpoint; AIOS now
recognizes work requests there and returns the same idempotent admission receipt.
The ingestion path owns admission independently of that optional relay.
No SillyTavern code or credentials decide the destination. This pass does not add a
native function registration to SillyTavern. A future adapter can use the native
schema/API below; plain text in the HUD alone does not register a provider tool.

Requested constraints are bounded text appended to the objective. They are not
filesystem enforcement or arbitrary executable checks. Verification commands,
expected outputs, preserved paths and budgets come from the operator's pinned
profile. Goal identity is pinned from the origin's unique active open attempt at
initial admission, if exactly one exists. With none or multiple active attempts,
work is standalone and cannot automatically close any goal. A later attempt never
replaces a pinned one. An unrecognized capability returns needs_configuration;
AIOS never falls back to an unrelated machine.

## Native and internal callers

`frame.work.tool` contains an OpenAI-style `request_work` schema. A compatible
integration can register it and relay the call with host metadata to:

`POST /agent/instance/{instance_id}/work/request`

The body contains the schema arguments plus stable `request_id` and the current
`source_node_id`, both supplied by the integration rather than the model. The
instance path comes from its authenticated session context. Native requests require
`Authorization: Bearer <AIOS_WORK_REQUEST_API_KEY>`. Configure a distinct integration
key; it must differ from both workspace worker and review keys. It permits requests,
not profile configuration, review or policy changes. Keep credentials out of model
context. Like existing integration keys, this is a trusted deployment-wide key,
not per-user authentication; do not give it to untrusted tenants.

Internal AIOS callers use `WorkAdmission.request` or the registered `work.request`
action. Both enter the same admission service, so a planner and a conversational
request do not create two execution mechanisms. The existing bounded goal-planning
research loop is not expanded into a general machine controller in this pass.

A typical response is:

```json
{"request_id":"...","status":"queued","reason":"Assignment admitted; awaiting worker claim","run_id":"...","completed":false}
```

Other admission states are waiting_client, awaiting_approval, needs_configuration,
denied, invalidated, and requested. Malformed native requests return validation
errors directly to the tool host. Neither queued nor completed model prose proves
execution. Existing run checks, evidence and reviewed goal completion stay intact.

Pending requests are reconsidered during authenticated v3 client polling. Offline
clients leave work waiting; bringing a listener online can admit eligible requests.
The HUD itself only reads state. Original source ancestry, link, profile, policy and
open goal attempt are revalidated before queueing. Criteria and destination are
pinned once selected. Uncertain/expired executions are never replayed automatically.
Request IDs, source receipts and concurrent identical in-flight intents deduplicate;
retry aliases keep pointing at the original receipt even after the run finishes.
There is no server push, automatic generation interruption, or client wake-on-LAN.

Tests exercise real workspace lifecycle SQL with minimal independent cognition
fixtures, plus native-route credential separation and HUD rendering. Native
PostgreSQL CI additionally checks concurrent duplicate submissions. Local PGlite
checks do not establish live model or production SillyTavern reliability.
## Read-only experimental inspections

Explicit read-only/no-file constraints in a request now select direct JSON
reporting for the standard `workspace / experimental_task` preset. Admission
removes only the exact built-in experiment-report output/check pair before sealing
the assignment, and keeps budgets, policy, approval and routing rules intact.
Already read-only profiles with no outputs/checks are also supported. A custom
output/check conflict returns `needs_configuration`; the character cannot bypass
custom operator checks. Existing queued assignments are immutable and must be
replaced with a fresh request if they contain the old contradictory file-report
requirement.

Use the updated AIOS-Client for enforced inspection mode and `inspect_environment`.
Recognized wording is a conservative restriction convention, not a general policy
language or an OS sandbox. Inspection mode denies arbitrary shell/code execution,
stdin, writes, Git subprocesses and HTTP tools; host audit logging continues.
The HUD prioritizes submitted failure/stop reasons and bounded provider finish
metadata before partial findings. Missing failure details are stated explicitly;
partial worker observations never establish task success.
