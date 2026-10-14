# Connect your application to AIOS

Run AIOS using [Installation](installation.md). The application you are building
can run in another process or on another machine. It sends HTTP requests to AIOS
and uses the returned HUD as context for its chosen model.

AIOS stores persistent state; your application owns its interface, model call,
and turn ordering. Registering an AIOS inference provider is optional for this
client loop. It is used for AIOS's own cognitive work.

## Run the Python example

From the repository root, with AIOS already running:

```bash
python3 examples/live_client.py --conversation developer-demo-001 --turn 1
```

This creates/resumes a session, bootstraps two name-only demo identities,
activates them in one world, stores a user message, and prints a ready HUD.
It uses no third-party client dependencies and makes no LLM call. To exercise
recording a response, supply text from your model:

```bash
python3 examples/live_client.py --conversation developer-demo-002 --turn 1 \
  --message 'The brass key is on the kitchen table.' \
  --reply 'I will look for the brass key on the kitchen table.'
```

Use a new `--turn` for each new exchange. Keep `--conversation` stable on
reconnect. Rerunning an older turn after later messages have advanced the source
head is not a request to rewind the conversation; the example stops on a conflict.

Replace the marked model-call block in [live_client.py](../examples/live_client.py)
with your provider code. Supply `hud['text']` as context alongside your
application instructions and current conversation. Ingest the actual generated
response, then persist the returned coordinates before advancing to the next turn.

## Know your identifiers

| Field | Owner and meaning |
|---|---|
| `source_session_id` | Your application's stable conversation key; pair it with a stable `source` |
| `session_id` | UUID returned by AIOS for that conversation |
| `character_id` | Stable identity key, such as `docs-guide`; not an instance UUID |
| `user_name` | Human actor key used in routing; keep stable and distinct from `character_id` |
| `instance_id` | UUID of a character running in a world |
| `world_id` | UUID returned by activation; pass it to other participants sharing that world |
| `timeline_id` | Runtime or source timeline; the activation and ingestion values need not match |
| `node_id` | Source coordinate returned by ingestion; pass this to the HUD request |
| `event_id` | Integer ID of the durable input event |
| `scope_key` | Routing scope; use the same explicit value in activation and ingestion |

Humans and characters share the runtime model. Their controller types differ.
The tutorial explicitly creates and activates both before submitting messages.
The activation endpoint returns 404 for an unknown identity.

## Equivalent curl walkthrough

These commands use Bash, curl, and `jq`. Run them sequentially in one shell.
They write demo state to your AIOS installation. Change the conversation key to
start another conversation.

```bash
BASE=http://127.0.0.1:8000
SESSION=$(curl --fail-with-body -sS "$BASE/session" \
  -H 'Content-Type: application/json' \
  -d '{"source":"developer-demo","source_session_id":"curl-demo-001","topic":"First integration"}' | jq -er '.session_id')

for ACTOR in docs-guide docs-human; do
  curl --fail-with-body -sS "$BASE/character/$ACTOR/identity/bootstrap/card" \
    -H 'Content-Type: application/json' \
    -d "$(jq -n --arg name "$ACTOR" '{card:{name:$name},source_name:"developer-demo",replace_authored_facets:false,auto_accept_authored:false}')"
done

ACTIVATION=$(curl --fail-with-body -sS "$BASE/character/docs-guide/activate" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n --arg session "$SESSION" '{session_id:$session,user_name:"docs-human",scope_key:"conversation",controller_type:"agent"}')")
INSTANCE=$(jq -er '.instance_id' <<< "$ACTIVATION")
WORLD=$(jq -er '.world_id' <<< "$ACTIVATION")

curl --fail-with-body -sS "$BASE/character/docs-human/activate" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n --arg session "$SESSION" --arg world "$WORLD" '{session_id:$session,world_id:$world,user_name:"docs-human",scope_key:"conversation",controller_type:"human",controller_ref:"docs-human"}')"

INGEST=$(curl --fail-with-body -sS "$BASE/ingest" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n --arg session "$SESSION" '{session_id:$session,character_id:"docs-guide",user_name:"docs-human",scope_key:"conversation",speaker_type:"user",speaker_id:"docs-human",recipient_id:"docs-guide",text:"The brass key is on the kitchen table.",kind:"chat_message",payload:{source:"developer-demo"},dedupe_key:($session+":turn-1:user:revision-1")}')")
NODE=$(jq -er '.node_id' <<< "$INGEST")

curl --fail-with-body -sS -X POST \
  "$BASE/instance/$INSTANCE/hud?through_node_id=$NODE&wait_ms=2500" | jq .
```

Read `generation_ready` before using `text`. If false, retry that HUD request
with bounded backoff while no new message advances the conversation. A 409
requires inspecting the source head, not substituting `/frame` or dropping
`through_node_id`. The Python example implements a conservative retry policy.

## Record the model response

```bash
curl --fail-with-body -sS "$BASE/ingest" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n --arg session "$SESSION" '{session_id:$session,character_id:"docs-guide",user_name:"docs-human",scope_key:"conversation",speaker_type:"character",speaker_id:"docs-guide",recipient_id:"docs-human",text:"I will look for the brass key on the kitchen table.",kind:"chat_message",payload:{source:"developer-demo"},dedupe_key:($session+":turn-1:character:revision-1")}')"
```

Only submit this after receiving a ready HUD; in your application replace the
sample text with the actual output. A sentence expressing an intention is input
to cognition, not a guarantee that AIOS will create or display a goal.

## Ordering, retries, and reconnects

Serialize ingestion and generation per conversation. Persist the session,
instance, source node, and your message keys in your application. Reuse
`source` and `source_session_id` to resolve the same session after restart, and
reactivate with the same identity, user, and world/session coordinates.

Use an explicit `dedupe_key` per logical message revision, namespaced by session.
Retry an uncertain submission with the same key and identical request. Distinct
turns with identical text need distinct keys: the generic default key is based
on session, kind, speaker, and text, so repeated text can otherwise collapse.
Do not reuse a key for changed content.

Generic ingestion does not promise transcript-edit replacement semantics.
SillyTavern's source/message-slot handling is a specific integration; setting an
arbitrary `payload.message_id` does not give every client that behavior.

A replay may return `source_current:false`. It acknowledges an existing event;
it does not mean the old event became the active source head. Inspect state and
resume the appropriate turn. Treat transport timeouts separately from API
validation errors, and bound all retries.
