# Project notebooks — Pass A

AIOS stores **private, append-only working notes** for a managed goal on a source timeline.
This is separate from episodic memory, RDF assertions, research evidence, and AIOS
Client's workspace memory. Notes are attributed statements, **not verified facts**
or evidence of goal completion.

## Deployment

Apply `migrations/current/20261120_01_project_notebooks.sql` using the usual
migration procedure **before** restarting the API. Pass A does not require
Qdrant, a new inference provider, or an embedding service.

## Character-authored notes

A character may emit **one** non-quoted structured action outside code fences:

```xml
<aios_action>{"op":"notebook.append","goal_ref":"COPY_ACTIVE_GOAL_REF","entry_type":"question","content":"Check whether the PDF actually supports this claim."}</aios_action>
```

The goal reference must resolve to a managed goal with a currently active
attempt on the instance's current source timeline. Admission requires a genuinely
new, character-authored ingested source event, with no superseded source event.
Existing one-action-per-message parsing rules apply. The note is persisted
with a source-node attribution and an idempotency key.

Allowed entry types: `note`, `observation`, `question`, `decision`,
`next_step`, `worker_report`. Maximum content length: 4,000 characters.
The server determines instance, timeline, author and receipt identity; these
cannot be supplied in the action. A note is not automatically injected into
a chat HUD.

## Native project-work interface

The AIOS server exposes the following routes for native workspace agents:

- `POST /agent/instance/{instance_id}/notebook/append`
- `POST /agent/instance/{instance_id}/notebook/read`

They require `Authorization: Bearer <AIOS_WORK_REQUEST_API_KEY>` with the same
distinct-credential policy as native work requests. In addition, the supplied
`work_request_id` must belong to that instance, goal and current timeline,
must match the open goal attempt, and must refer to a **running** workspace run.
The key by itself never grants access to an arbitrary character notebook.

Example append body:

```json
{
  "work_request_id": "WORK-REQUEST-UUID",
  "note": {
    "goal_ref": "GOAL-UUID",
    "entry_type": "observation",
    "content": "Calibre returned two records; the complete passages were not obtained."
  }
}
```

Example bounded read/search body:

```json
{
  "work_request_id": "WORK-REQUEST-UUID",
  "query": {
    "goal_ref": "GOAL-UUID",
    "query": "glaciation deposits",
    "limit": 10
  }
}
```

Omit `query` for recent notes. Search uses scoped PostgreSQL full-text indexing,
not Qdrant; `before_id` in a subsequent request pages backward in creation
order. Requests are capped at 30 entries and at most 12,000 note-content characters (6,000 by default), via the optional `max_chars` field.

The native endpoints are the AIOS-side contract for a future AIOS Client
notebook tool adapter. This change does **not** automatically install tools
into AIOS_Client's separate worker runtime.

## Scope and follow-up passes

The notebook identity is `(instance_id, source_timeline_id, goal_id)`; it
survives retries and revisions of the same managed goal. Raw notes are immutable.
The character side presently supports **append action tags**; reads/searches
are through the authorized native API. Providing character-directed retrieval
receipts in the HUD is a separate task and should be demand-driven.

No background consolidation, automatic note extraction, semantic retrieval,
or persistent notebook HUD section is enabled by this pass. Source/worker
authorship and original note text are retained for future curated summaries.

## Pass B: curated, versioned summaries (2026-11-20)

Apply `migrations/current/20261120_02_notebook_consolidation.sql` **after**
`20261120_01_project_notebooks.sql`, then restart both API and background
`runner_v2` processes. The new `notebook_consolidate` pipeline job executes
through the existing executive inference provider and BACKGROUND lane; no
inference is added to ordinary chat / HUD rendering or to the note insert.

After a successful new note insertion, AIOS checks how many notebook entries
are not yet covered by the latest curated summary. At least **5** new entries
queues maintenance. A duplicate source replay does not schedule maintenance.
One active job per notebook is permitted; each job processes up to 12 batches
of 4 notes. Further backlogs can be explicitly queued again.

The client-facing native API, restricted to the same running project work
request and active goal attempt as Pass A, additionally supports:

- `POST /agent/instance/{instance_id}/notebook/summary` — latest curated
  revision, or `unconsolidated`.
- `POST /agent/instance/{instance_id}/notebook/consolidate` — explicitly
  enqueue maintenance even when fewer than 5 notes are pending.

Both use the same `WorkerNotebookRead` body as `/notebook/read`;
supply `work_request_id` and a nested `query.goal_ref`. For example:

```json
{"work_request_id":"WORK-REQUEST-UUID","query":{"goal_ref":"GOAL-UUID"}}
```

Each inference receives the prior curated summary (if present) plus a bounded
batch of 4 raw entries, retaining source note IDs and author kinds. The model
returns a summary, note-referenced working findings, open questions, and
next steps. Only existing note references owned by that notebook are accepted.
The output is labeled curated/derived **not verified evidence**. Every accepted
revision is stored separately with `covered_count`; stale concurrent
completions are rejected by a transactional source-revision check. Original
entries remain unchanged.

No Qdrant indexing, automatic claim promotion to RDF, or permanent HUD injection
occurs in Pass B. This is deliberately an opt-in **project maintenance** path,
not a general memory consolidation service. Its output is a convenience index,
not a replacement for the raw notes or a verified source archive.

Note: The native routes are on the AIOS server. The separate AIOS_Client
worker runtime still requires a tool adapter to call them. Running DB and
live inference integration tests remains necessary before production use.
