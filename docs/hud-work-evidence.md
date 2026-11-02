# Goal and worker evidence integration

Work requests may include `goal_ref`, copied from the HUD's managed goal list.
AIOS resolves the current open attempt and validates the instance and source
timeline. Requests without a reference retain the single-active-goal fallback;
when several goals are active, the receipt explicitly reports an unlinked task.
The reference grants no execution authority and cannot create a goal.

The planner receives up to three terminal worker receipts from the six most
recent visible requests. Receipts for another goal or an older attempt are
excluded; unlinked receipts are labeled contextual evidence. Their revisions
participate in planning freshness, including late-result and continuation fences.
Execution outcome, reported findings and goal completion remain separate.

## Read retained results

Use the native `read_work_result` tool or one block in a character reply:

```json
{"op":"read_work_result","run_id":"COPY_RUN_ID","section":"findings","cursor":0,"limit":3000}
```

For chat transport, wrap the JSON in `<aios_action>` and `</aios_action>` outside
Markdown fences. Supply exactly one `request_id` or `run_id`. Sections are
`summary`, `findings`, `limitations`, `requested_content`, `report`, and `checks`.
Continue with `next_cursor` and the returned `revision`; a changed revision
requires starting at cursor zero. Pages contain at most 4000 characters.

Native HTTP clients can POST the same fields (without `op`) to
`/agent/instance/{instance_id}/work/result`, using the existing distinct
`AIOS_WORK_REQUEST_API_KEY`. Internal agents use the registered `work.result`
action. Reading neither submits a workspace run nor requires an online worker.

Results are restricted to requests owned by the conversation instance on its
current source timeline, with a nonsuperseded source node reachable from its
source head (bounded to 512 edges). Assignment templates, binding credentials,
and operator decisions are not included. Worker content stays untrusted.

## Review old parser goals

The new policy prevents current-utterance framing and malformed objectives from
becoming managed goals. Existing goals are not silently deleted. Before source
cognition is rebuilt, review eligible stored units with:

```bash
python -m aios_app.epistemic.goal_revalidation --instance INSTANCE_UUID
```

Add `--apply` to cancel the listed goals through the normal lifecycle. This
preserves source units and records policy version and source evidence in the
resolution metadata. Only the two new rejection reasons are repaired; goals
with missing original units and operator-authored goals require separate review.
The operation is idempotent. No new database migration is required.
