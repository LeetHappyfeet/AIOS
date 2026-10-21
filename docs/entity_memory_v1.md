# Entity memory RDF anchors

AIOS can preserve people, places, and nominal object mentions as source-grounded
RDF anchors without a second NLP pass or an LLM request. This projection uses
the already persisted spaCy parse, not the permissive textual entity keys in
`claim_semantic_frame`. It does not mutate world entities, physical state,
character trust, beliefs, access grants, or the original extracted frames.

## Runtime

Apply the normal migration chain (`python -m aios_app.migrate`) and restart the
semantic index and topology processes. Migration `20261020_01_entity_memory.sql`
queues existing linguistic projections for bounded backfill. The semantic-index
loop builds at most its background batch size every ten seconds, capped at 16
sections. Topology projects at most one source graph per cycle. No new service,
model, embedding request, or external inference provider is required.

Both steps can also be called directly:

```python
from aios_app.entity_memory.service import (
    collect_entity_memory_once, project_entity_memory_once,
)
await collect_entity_memory_once(db, limit=4)
await project_entity_memory_once(db, fuseki, limit=1)
```

The English language vocabulary reconstructs cached `Doc` annotations; it does
not load or execute the trained model. An absent/stale parse or unresolved
source context defers collection rather than launching another parse or
publishing to the world dataset. Loss of context withdraws a previously built
graph. Malformed cached sections back off independently so they cannot starve
other sections. Context updates wake deferred sections.

## Admission and identity

- Exact registered character IDs, display names, canonical names, and aliases
  take priority over NER type guesses. Conflicting registry matches abstain.
- Explicit chat speaker/recipient identifiers can anchor unregistered people.
  Source-document consumption does not bind corpus names to runtime identities.
- Stable UUIDs include owner/world/timeline scope and identity material, never
  the NER type or current display label. Separate timelines do not silently share
  identities. This pass does not authorize cross-chat memory transfer.
- Noun chunks cover ordinary objects and places. Candidate anchors are marked
  uncertain; an unknown type is allowed. Small exclusion sets remove common
  abstract nouns and body parts. They are conservative filters, not a complete
  classifier of physical objects.
- New nominal introductions and unregistered names are source-occurrence scoped.
  Within one source, a definite description links only to a unique compatible
  earlier nominal. Multiple compatible antecedents remain unresolved. Two
  indefinite mugs are two candidates. Cross-message object coreference and
  third-person pronoun resolution are intentionally deferred.
- Mentions retain character offsets, heads, NER labels and rejection reasons.
  `*`, interjections, adjectives, moments and distances cannot become entity
  anchors merely because an earlier frame generated a textual key for them.
  At most 256 mentions are collected per section; truncation is recorded.

## Relationships and RDF ownership

The initial vocabulary covers explicit `own`/`possess`/`have` constructions and
copular locations (`in`, `at`, `inside`, `on`, `near`). Supported grammatical
endpoints must have anchors. Each relationship is reified evidence with source
text, predicate offset, polarity, scope, modal cues, and enclosing predicate
attribution, tense and aspect. Negation of a reporting/belief predicate stays
with that enclosing predicate and does not invert its embedded relationship. It is never a bare authoritative `owns` or `locatedAt` triple.
Possessive pronouns alone do not establish ownership. Existing semantic-frame
and Topic Atlas output is preserved; this pass creates a dedicated improved
anchor projection rather than rewriting historical frame identities.

Graphs are named `urn:aios:entity-memory:section:<section UUID>`, using vocabulary
`urn:aios:entity-memory#`. Entity IRIs are
`urn:aios:memory-entity:<stable UUID>`. The source records owner, world, timeline,
DAG node, section, original context qualifications, and `evidence_only` authority.
Mentions connect to anchors through `refersTo`; `hasRelationEvidence` connects to
qualified relationship records. These are derived navigation/memory graphs, not
the asserted-world graph or the existing character belief graphs.

Character-owned and uncertain sources stay in the `char` dataset. A source can
enter `world` only when every resolved context explicitly has world scope and
neither source nor contexts names a character owner. Mixed ownership never
defaults to public. Graph dataset placement itself grants no character knowledge.

## Lifecycle and inspection

SQL revisions queue source parse changes, context changes, supersession and
source deletion. Source tombstones survive deletion until the previous RDF graph
is cleared. Registry name/alias edits use an indexed name inventory to invalidate
only affected snapshots. RDF writers lock one projection through external write
and receipt; failure/cancellation does not acknowledge the revision. Each refresh clears the non-target dataset before inserting the target graph;
tombstones clear both deterministic datasets. This also cleans up a write that
reached Fuseki just before its SQL receipt was lost. Retries safely
repeat graph replacement. Unchanged sources perform no RDF writes.

Inspect pending work:

```sql
SELECT count(*) FILTER (WHERE built_revision < revision) AS build_pending,
       count(*) FILTER (WHERE rdf_revision < built_revision) AS rdf_pending
FROM aios.entity_memory_projection;
```

Inspect recent mentions and relationships:

```sql
SELECT section_id, dataset, payload->'anchors' AS anchors,
       payload->'mentions' AS mentions, payload->'relations' AS relations
FROM aios.entity_memory_projection
ORDER BY updated_at DESC LIMIT 10;
```

To deliberately rebuild these derived snapshots after a policy change:

```sql
UPDATE aios.entity_memory_projection
SET revision=revision+1, retry_after=now(), updated_at=now();
```

This first pass adds inspectable RDF anchors. HUD retrieval and planning do not
yet consume these graphs automatically. More elaborate social relationships,
object coreference across messages, and full temporal interpretation need later
work and should preserve this evidence/authority separation.
