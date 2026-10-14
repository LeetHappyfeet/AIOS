# Shadow character-conditioned participation experiment

[Back to developer documentation](README.md)


This opt-in lexical evaluator proposes `background`, `latent`, or `foreground`
participation for claims acquired by one character instance. It writes only an
experiment queue and audit ledger. It does not alter eligibility, semantic
admission, Qdrant, neighbor relations, or clustering. It is an attention proxy,
not a significance model or a truth verifier.

Apply current migrations through the normal launcher, then enroll an instance:

```sh
curl -X POST http://localhost:8000/agent/instance/INSTANCE_UUID/participation/experiments \
  -H 'Content-Type: application/json' \
  -d '{"duration_minutes":60,"max_claims":100}'
```

Keep the returned `experiment_id`. By default sampling starts now. Optional
`since_at` is an ISO timestamp with a timezone, at most seven days in the past;
backfill samples the most recently updated acquired claims in that interval.
Enrollment captures acquisition/knowledge updates, including claims whose
context has not resolved yet. Each claim is queued once per experiment.
The cap includes deferred, skipped, and failed claims.

Run the independent worker from the parent of the `aios_app` package using the
same environment/database configuration as AIOS:

```sh
python -m aios_app.agent.participation
# Or process at most one batch:
python -m aios_app.agent.participation --once
```

The worker processes at most 16 items per batch with a five-second soft budget,
a 1.5-second SQL statement timeout, and at most two database connections.
It does not wait for the semantic classifier. Context is capped at 64 active
goals, 64 active identity facets, and 64 relationships. Missing identity context
or truncated context prevents a confident background decision. Direct character
involvement, lexical goal/identity overlap, repeated known propositions, and
known conflict candidates provide attention hints. Relationship matches alone
propose latent participation. Conflict hints never assert a contradiction.

Inspect the experiment:

```sh
curl http://localhost:8000/agent/instance/INSTANCE_UUID/participation/experiments/EXPERIMENT_UUID
```

The response includes queue status, population counts, proposed foreground
share, mean evaluation time, and up to 50 decisions (query `?limit=200` for more).
Every decision records claim inputs, epistemic status, reason codes, signals,
the current character context snapshot, its hash, and policy version.
Historical backfill uses **current** character context; it is not a historical
replay. Recurrence/conflict evidence is restricted to acquired claims observed
no later than the sampled claim. Unresolved claims retry every 30 seconds and
expire ten minutes after they were queued; evaluation failures stop after three
attempts. Normal enrollment expiry stops sampling but lets queued work drain.

To stop both sampling and processing immediately:

```sh
curl -X POST http://localhost:8000/agent/instance/INSTANCE_UUID/participation/experiments/EXPERIMENT_UUID/stop
```

Review examples from all three populations, especially proposed background
claims, before considering any admission change. Foreground share measures
proposed selectivity; usefulness, false exclusions, and meaningful-edge yield
still require review or a later comparison experiment.
