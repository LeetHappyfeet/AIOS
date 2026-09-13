# Causal Integrity Kernel

AIOS separates objective branch consistency from semantic and epistemic state.

The causal kernel answers one narrow question:

> Can this proposed objective state transition coexist with the committed history of this world and timeline?

It does **not** decide what a character believes, remembers, reports, or perceives.

## Boundaries

```text
untrusted language / documents / chat
              |
              v
      semantic interpretation
              |
      semantic candidates
              |
      explicit causal bridge
              v
+----------------------------------+
|       CAUSAL INTEGRITY KERNEL    |
|                                  |
| branch coordinate validation     |
| deterministic domain policies    |
| invariant / transition checks    |
| serialized commit                |
| immutable world_event ledger     |
+----------------+-----------------+
                 |
                 v
       objective /world projection
                 |
       perception / acquisition
                 |
                 v
                /char
                 |
                 v
                 HUD
```

Machine-authored deterministic sources enter below the language boundary:

```text
sensor / simulator / device / deterministic plugin
                     |
               typed event
                     |
                     v
             causal kernel
```

Natural-language `/observation` remains evidence and is never treated as deterministic truth merely because it came from an external source.

## `/world` and `/char`

`/world` is the semantic projection of branch-coherent committed objective state.

`/char` is subjective. It may contain beliefs, memories, testimony, mistakes, deception, conflicting evidence, and incomplete knowledge. `/char` is allowed to disagree with `/world`.

The causal package never writes:

- `character_knowledge`
- `character_proposition_knowledge`
- `knowledge_acquisition_event`
- `character_relationship`

The HUD never queries `causal_state` directly. Runtime location is exposed to HUD assembly only through the objective `/world` `located_in` projection.

## Storage

### `causal_candidate`

A typed proposal for objective reality. A candidate is not truth.

It records the exact world/timeline coordinate, domain, entity, proposed value, source provenance, and optional originating semantic claim/frame.

### `causal_admission`

The consistency decision for a candidate. Decisions are independent of semantic confidence:

- `ADMITTED`
- `ADMITTED_WITH_LATENT_TRANSITION`
- `REJECTED_IMPOSSIBLE`
- `CONFLICT`
- `UNDERDETERMINED`
- `FORK_REQUIRED`
- `EPISTEMIC_ONLY`

Rejected candidates remain auditable evidence; rejection does not delete their source claims.

### `world_event`

`world_event` is reused as the immutable ledger for admitted deterministic transitions. The causal migration adds domain, source, candidate, before/delta/result state, and state-version columns rather than creating a competing second event log.

### `causal_state`

Materialized current state used for fast validation. It is keyed by:

```text
world_id
timeline_id
domain_id
entity_id
state_key
```

Therefore one timeline cannot silently overwrite another timeline's deterministic state.

`causal_state` is a cache/snapshot of committed history; it is not a HUD or epistemic table.

## Concurrency and paradox prevention

Commits acquire a PostgreSQL transaction advisory lock over:

```text
world + timeline + domain + entity + state_key
```

The kernel then validates that the supplied world, timeline, entity, target entity, and DAG node belong to the same branch before evaluating the domain policy.

Optimistic `expected_state_version` checking prevents stale deterministic writes.

A replayed `candidate_id` returns the previous admission result and does not increment state twice.

## Built-in domains

### `world.location`

Physical location is exclusive per entity and timeline.

An explicit transition such as `move` can change location. A second incompatible endpoint assertion at the same causal coordinate is rejected. A later endpoint may be admitted with a latent transition only when the caller explicitly permits an omitted transition.

AIOS core does not hard-code real-world travel speeds or ban teleportation. World/domain rules can add those mechanics later. The core invariant is that a state change requires an admissible transition.

### `world.scalar`

Generic deterministic values for sensors, simulators, devices, and deterministic plugins.

Examples:

```text
room_temperature
battery_voltage
health
stamina
energy
simulator.position.x
```

Different values at the same causal coordinate conflict rather than silently using last-write-wins. Ordered later values are admissible. Units and measurement uncertainty can travel with the value envelope without becoming semantic confidence.

## Runtime integration

`WorldRuntimeService.apply_action()` no longer mutates `character_runtime_state.location_entity_id` and no longer inserts its own accepted `world_event` for movement.

The runtime path is now:

```text
controller action intent
       |
       v
runtime action-rule validation (causal/rules.py)
       |
       v
DAG intent node
       |
       v
CausalCandidate(world.location / move)
       |
       v
CausalIntegrityKernel.require_commit()
       |
       +--> causal_candidate
       +--> causal_admission
       +--> world_event (if admitted)
       +--> causal_state (if admitted)
       +--> objective /world projection
```

The old runtime action-rule implementation was removed so rule policy has one implementation.

Legacy runtime `location_entity_id`, `health`, `stamina`, `energy`, and `physical_state` are bootstrapped into the causal layer by migration and then cleared as mutable runtime authority. Emotional/social/goals/tasks remain subjective `/char` runtime state.

## Semantic compatibility without semantic authority

`causal/semantic_bridge.py` can compile a resolved spatial semantic frame into a `CausalCandidate` and call `kernel.evaluate()`.

This gives semantic disambiguation a new signal:

```text
linguistically plausible?
entity resolution plausible?
causally compatible with this branch?
```

The bridge never commits automatically. Narrative, character speech, beliefs, and source documents remain evidence until an explicit world-admission path proposes them to a concrete branch.

## Sensors and simulators

`causal/ingress.py` exposes `DeterministicIngress` for typed machine state.

Example shape:

```python
await ingress.commit_scalar(
    world_id=world_id,
    timeline_id=timeline_id,
    entity_id=room_id,
    state_key="temperature",
    value=19.2,
    unit="C",
    uncertainty=0.1,
    source_kind="sensor",
    source_ref="thermometer-4",
    occurred_at=sample_time,
)
```

A simulator can submit location or scalar state with `source_kind="simulator"`. These inputs bypass NLP but still pass causal coordinate/invariant checks.

## Extension rule

New deterministic plugins belong under the causal subsystem, not the HUD plugin subsystem.

A deterministic plugin may register a `CausalDomain` with `CausalDomainRegistry` or use the generic scalar ingress. HUD plugins may render epistemically projected consequences but should not read or mutate `causal_state`.
