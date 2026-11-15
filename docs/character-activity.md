# Character activity context (v1)

This is an optional, character-owned presentation layer for v3 workspace work.
It does **not** change workspace authorization, assignment hashes, the technical
tool loop, host verification, or goal review.

## Operation

1. Update AIOS and AIOS_Client together and apply the additive
   `20261115_01_character_activity.sql` migration.
2. On its v3 context request, an updated client requests
   `character_activity: true` only when AIOS advertises
   `character-activity-v1`. Old clients do not incur an inference call.
3. AIOS reads the original request's source node and up to three direct
   ancestors on its pinned conversation timeline, plus the work objective.
   It uses the AIOS inference broker with worker class
   `workspace_activity` to generate a short first-person JSON brief.
   Configure an enabled donated inference provider for that class, or one
   that accepts unlisted classes. Missing providers and malformed answers
   leave ordinary workspace execution available.
4. AIOS caches the brief on `workspace_run.character_activity`; further
   context requests reuse it. The brief never enters the immutable assignment,
   never authorizes commands, and never verifies a narrated event.
5. AIOS_Client uses it in a separate no-tools planning call. Only the
   technical plan (not the narrative brief) enters the repeated tool loop.
   Technical recall and prior work evidence remain available through their
   established scoped tools.
6. After independent host checks, a separate no-tools review/report call
   produces a first-person `character_report` under the final run result.
   Review failure is nonfatal. This report cannot override the underlying
   verification status or create a goal completion.
7. The character HUD presents the generated interpretation under
   **MY COMPUTER — CHARACTER ACTIVITY**, while preserving the original
   worker evidence and its limitations.

This first pass does not create an initiative scheduler, new memory claims,
recursive continuations, or execution privileges. It is intended to test
whether character intent influences plans and whether reported outcomes
lead to more coherent future interaction.

## Boundaries

- A source statement like "I studied Python all night" remains narration,
  not proof of actual terminal use or gained expertise.
- The AIOS-owned brief is bounded and associated with the run's immutable
  origin; a changed chat does not silently rewrite an in-flight task.
- A worker's first-person report is interpretation, not independent evidence.
- Planning and review use the same client model endpoint, with **no tools**.
  If that endpoint rejects a no-tools chat completion, auxiliary processing
  is skipped and the technical work still proceeds.
- A cancelled or timed-out run is not retried as a new command merely because
  the optional character report was unavailable.

## Tests

- AIOS: `pytest tests/test_character_activity.py`
- AIOS_Client: `pytest tests/test_character_activity.py`

For a controlled comparison, run the same bounded task with an older client
(or without a configured activity-class provider) and with an updated client.
Compare work accuracy, host checks, tokens, and the character's subsequent
ability to discuss outstanding design decisions.
