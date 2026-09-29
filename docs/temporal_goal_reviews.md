# Future goal reviews

Character-owned plans can be parked as scheduled goals and returned to active
attention by the temporal trigger worker. A due trigger only reactivates the
goal and emits `GOAL_REVIEW_DUE`; it does not execute the planned action.

The message cognition worker may propose a timer for a positive character-owned
plan or commitment. AIOS accepts that proposal only when the time phrase occurs
in the character's own excerpt and the deterministic resolver can resolve it
without inventing a time. Supported calendar windows are `today` or `tomorrow`, with
optional `morning`, `afternoon`, `evening`, or `tonight`. Supported relative
durations are `in N minutes`, `in N hours`, `in N days`, and `in N weeks`.
For vague future phrases such as `later` or `soon`, the local cognition worker
can choose a revisit interval from 15 minutes, 1 hour, 4 hours, or 1 day when
nearby context supports one; AIOS validates that bounded choice. Otherwise the
goal stays active without a timer. A rejected or unresolved proposal never
parks the goal.

Calendar phrases need an IANA timezone. Set `AIOS_DEFAULT_TIMEZONE` for the
installation, or set `character_instance.meta.timezone` for an individual
instance. An instance value takes precedence. For example:

```sql
UPDATE aios.character_instance
SET meta = COALESCE(meta, '{}'::jsonb)
            || jsonb_build_object('timezone', 'America/New_York')
WHERE instance_id = '00000000-0000-0000-0000-000000000000';
```

Relative durations are anchored to the source event timestamp and do not need
a timezone. Calendar windows retain their local timezone and both UTC window
bounds on the temporal trigger. The HUD lists scheduled goals separately from
active goals; at the start of the window the trigger checks that the goal is
still scheduled, reactivates it, and wakes the existing bounded cognition loop.
