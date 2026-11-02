# Goal scheduling by instance activity

Routine advisory planning and goal continuation now follow external interaction
with the individual conversational instance. Running a worker, renewing foreground
lineage, polling a workspace client, or receiving tool output does not refresh
this timestamp. A genuinely new, adopted user/character chat message does.
Replays retain their existing ingest short circuit.

| Activity | Routine admission |
| --- | --- |
| Chat within 15 minutes | Active; at most one admission of each kind per minute |
| Chat within one hour | Idle; at most one admission of each kind per five minutes |
| Older or never used | Dormant; routine planning and continuation parked |
| Operator enables background goals | Eligible while dormant, at background priority |
| Authorized timer actually fires | Eligible for a 15 minute scheduled window |
| Runtime paused | Ineligible regardless of activity or background policy |

These are scheduling defaults, not changes to permissions, goal ownership,
world state, memory boundaries or workspace authorization. Timers and explicit
operator background policy can continue work without an open conversation.
Timers themselves still fire through the existing temporal scheduler.

The queue rechecks eligibility when claiming goal continuations and planning
inference, and the handlers check again before expensive work. Dormant queued
work is retained. New interaction resumes existing waiting needs immediately.
Continuation admission chooses one plan per instance per pass. Active queue
selection rotates by least recent continuation service rather than newest chat.
The runner's existing every-fourth backlog selection permits eligible idle or
background work to progress alongside continuously active conversations.

Unchanged continuation evidence uses 60, 120, 240, 480, then at most 900 seconds
between checks. Source/evidence changes reset the sequence when checked;
new chat resets it immediately. Query timings are excluded from the signature.
This remains bounded polling for evidence changes, not a new event bus.

## Operator controls and diagnostics

Character Control → Diagnostics → Runtime / Worlds shows the goal activity tier,
last external interaction, eligibility and background policy alongside each
instance. Copy the intended instance ID into the goal scheduling policy control
to enable or disable dormant background goals. This switch does not unpause a
runtime or grant execution permissions.

The same state is available in `aios.instance_goal_scheduling`; admission and
start timestamps make time spent waiting distinguishable from inactivity.
`LIVE READY` continues to describe the live pipeline, not goal completion.
Global semantic indexing and historical memory maintenance keep their existing
budgets; this change removes recurring dormant goal pressure, not all database
contention.

The standard launcher migration path applies
`20261101_01_goal_activity_scheduling.sql` before updated workers start. Existing
foreground lineage `updated_at` values seed external activity once; foreground
lease renewal does not update this field. Stop old processes before upgrading
so older workers cannot continue admitting dormant work under the old policy.
