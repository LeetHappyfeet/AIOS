# Character identity kernel

AIOS treats character identity as a slow-changing definition of the persistent
actor identified by `character_id`. Identity is not runtime state, memory,
belief, mood, or world truth.

## Boundaries

- `character_id` is the stable system identity of the actor.
- `character_identity` is the driver's-license/root record.
- `character_identity_source` preserves imported authored/reference material.
- `character_identity_candidate` is the mandatory staging boundary for
  interpreted source material.
- `character_identity_facet` contains accepted durable identity.
- `character_identity_revision` records every accepted change and version.
- `compiled_identity_kernel` is the deterministic, character-scoped runtime
  projection shared by all active instances of that character.

Ordinary conversation ingestion, belief reconciliation, memory retrieval, and
`character_runtime_state` do not write identity.

## Stability

Facets use four stability classes:

1. `structural`: system identity; effectively immutable.
2. `constitutional`: canonical body/origin/fundamental identity; explicit
   revision only.
3. `core`: personality, values, characteristic expression; explicit and
   deliberately conservative revision.
4. `developmental`: long-term self-development. Experience may propose these
   but does not directly accept them.

Mutability is independently represented as `locked`, `explicit`, or
`developmental`.

## Bootstrap

Character cards are external authoring inputs, not AIOS runtime prompts.
The original payload is retained as a source. Description, personality, and
example-dialogue fields are staged as provenance-backed identity candidates.
Scenario and first-message fields are deliberately excluded from identity.

Authored character cards may auto-accept their candidates because importing the
card is an explicit identity-authoring operation. Set `auto_accept_authored`
false to inspect candidates first.

Other sources (wiki pages, biographies, canonical references, manual edits, or
developmental proposals) use the generic identity-source staging API. They do
not auto-accept. Their candidates carry authority, perspective and optional
continuity keys so reference knowledge cannot silently become self-knowledge or
cross incompatible canons.

## Perspective

Candidate perspective is one of:

- `self`: appropriate for the character's self-definition.
- `biographical`: narrator/reference fact; not automatically self-known.
- `public_reputation`: what others commonly say about the character.
- `secret`: source fact that must not become self-knowledge by accident.
- `unknown`: unresolved perspective.

Perspective is provenance/control metadata. Acceptance into identity remains an
explicit operation.

## Runtime

Accepted identity increments `character_identity.identity_version`. The
kernel compiler reads only accepted active facets plus compatibility fields from
the root identity, materializes a deterministic projection, and caches it by:

`(character_id, identity_version, compiler_version)`

Multiple runtime instances therefore share one identity kernel while retaining
separate worlds, cognition and runtime state. A new identity version invalidates
the old cache key without coupling identity to instance state.

The HUD consumes the compiled projection. It does not select, reconcile, infer,
or mutate identity.

## Development

Long-term experience may eventually create `developmental` candidates through
the same staging API with `source_type=developmental`. That is intentionally
only a proposal mechanism. No event, memory, repeated behavior, or LLM
observation has direct authority to rewrite the identity kernel.

This keeps personality development possible while making identity the most
conservative mutable layer in the character architecture.
