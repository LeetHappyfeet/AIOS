# Pipeline liveness and parked work

Run the canonical migrations, including `20261101_02_pipeline_liveness.sql`,
before restarting Pipeline Runner. The migration replaces the eligibility view
with a non-null Boolean decision and adds indexes for telemetry time ranges.
It preserves queued jobs, goals, source evidence and background permissions.

The launch status now reports runnable `q`, running `r`, `parked`, `delayed`
and `waiting` counts. Parked includes dormant/paused goal jobs and missing
eligibility records. Waiting includes continuations held behind another running
continuation for the same instance. Runnable lag excludes all three categories.
For the reported two dormant queued jobs, expect `0q/0r lag 0ms parked 2`.
New conversation activity or explicit background authorization resumes eligible
work through the existing scheduling policy; worker polling does not do so.

Queue telemetry aggregates pending stages once per sample. Rate counters use
independent indexed range queries every 30 seconds instead of a combined
history predicate every five seconds. Its SQL session has a two-second statement
budget and a 250ms lock budget. In `pg_stat_activity`, its application name is
`aios:pipeline-telemetry`.

Continuations have a 45-second overall deadline, a three-second SQL statement
budget and a 750ms SQL lock budget during their mutation transactions. Token
cleanup after errors/cancellation has a five-second deadline. The 90-second
plan lease remains the recovery fallback if cleanup cannot reach PostgreSQL.

The runner attempts advisory locks without waiting; contention defers the same
job for 15 seconds. LOOKUP work has a separate two-connection pool with bounded
SQL and application name `aios:lookup:JOB_UUID`. The async execution (including
connection/lock acquisition) is limited to 60 seconds and is watched by the
queue heartbeat. Lease loss cancels execution; cancellation terminates that
job's private SQL pool. Updates relinquishing queue ownership are worker-fenced.
No success is reported for a cancelled execution. Thread-isolated handlers
remain outside this cancellation guard because cancelling a thread wrapper
does not stop its underlying work.

This patch prevents the identified indefinite waits and fixes the misleading
queue display. It does not establish the execution cost of the separate
acquisition-discovery and observation-selection queries on a deployed database;
those require live execution plans if SQL-budget warnings continue.
