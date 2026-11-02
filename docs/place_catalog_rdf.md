# Place and character reference catalogs

The catalog presents particular places and their related entities, preserving the
boundary between world records, reported locations, encounters, and reference
knowledge. It reuses existing extraction and identifiers. It does not create
objects, infer inventories, merge names, or turn narration into world truth.

## Representation

`urn:aios:catalog#Place` describes an individual place reference. A bedroom can
also carry the category `catalog:Bedroom`, under `catalog:Room` and `catalog:Place`.
That class is not a singleton bedroom. The bed and place retain independent IDs.
Broad place categories use existing world entity types or an unambiguous existing
parsed noun head; this is classification, not identity resolution.

Existing entity-memory graphs in `world` and `char`, and corpus-reference graphs,
gain typed entities, labels, `describedBy`, and `hasMembershipEvidence` links.
The new vocabulary is documented in `rdf/ontology/place-catalog.ttl`. It uses
local URNs and requires no public ontology service or additional NLP pass.

For example, a report that a bed is in a bedroom produces this shape (IDs shortened):

```turtle
@prefix catalog: <urn:aios:catalog#> .
<urn:example:bedroom-1> a catalog:Place, catalog:Bedroom ;
    catalog:hasMembershipEvidence <urn:example:source-1:relation:0> .
<urn:example:source-1:relation:0> a catalog:MembershipEvidence ;
    catalog:member <urn:example:bed-1> ;
    catalog:place <urn:example:bedroom-1> ;
    catalog:spatialRelation "in" ;
    catalog:membershipStatus "reported" ;
    catalog:source <urn:example:source-1> .
```

The original relation still supplies source text, offsets, negation, modality,
and attribution. There is no direct assertion that the bed is currently there.
Negative, modal, conditional, attributed, and illustrative evidence remains
navigable under `other_evidence`, not positive `contents`. Nearness is likewise
not containment. In/on/inside/at/part_of/contains remain distinct relationships.
Locations with no contents do not acquire defaults such as beds or merchandise.

## Character references and people

When every context in an entity-memory payload identifies the same observer
instance, its
RDF gains `hasEntityReference` records with mode `source_mention`. A shared
character name alone never supplies the instance. The character inspection view
also includes:

- `relationship_record`: an existing relationship for this exact instance.
- `encounter_record`: an existing `met` relationship with an unsuperseded source
  node on the selected source ancestry. This is an encounter record, not an
  inference that a historical meeting was independently verified.
- `corpus_reference_exposure`: a section previously exposed to this instance,
  still permitted by the current corpus ACL.
- `acquired_corpus_reference`: a consumed section with a current source-eligible,
  active semantic admission and reflection permission in the cognitive scope.

Exposure, acquisition and meeting are separate. World entities use the same
`urn:aios:world-entity:<UUID>` in world and character exports. Extracted mentions
retain their existing source identities; corpus references retain corpus IDs.
No label-based link or owl:sameAs joins a corpus person to a runtime person.
Such an identity correspondence requires separate evidence and is not inferred
by this pass. A grant alone does not make every entity in a corpus known.

## Inspecting current views

Operator endpoints return JSON by default or named-graph RDF with `format=trig`:

- `GET /world/{world_id}/entity-catalog?timeline_id={uuid}&head_node_id={uuid}`
- `GET /instance/{instance_id}/entity-catalog`

The world endpoint requires an exact world/timeline/head coordinate. It includes
only spatial records with an ancestor validity node or an explicit matching
`meta.timeline_id`, excluding branch-incompatible and closed records. Legacy
untagged relationships are not assumed to belong to the selected branch.
`world_record` describes existing SQL world records; this endpoint performs no
causal admission or new world mutation. Unconnected world definitions are not
invented into a scene. This world endpoint follows the existing operator API
trust model; do not treat it as a character perception grant.

The character view reads its own eligible source projections, relationship
records, and exposed/acquired reference sections. It does not copy world contents
or other characters' knowledge. Source hashes, current projection revisions,
supersession, ancestry, current corpus grants and explicit denies are checked in
SQL. Queries run in one read-only, repeatable-read transaction with a three-second
ceiling. No inference, embedding, Fuseki read/write, or acquisition occurs on GET.
Unknown instances/coordinates return 404; timeout returns 503, not an empty world.

The JSON contains `places`, `people`, `unplaced`, and `familiarity`. Each place has
`contents` and `other_evidence`. Memberships carry source IDs and status. These
are bounded views: up to 24 personal sources, 24 relationship records, 24 corpus
sections and 128 world edges; ancestry is bounded to 256 edges. Corpus discovery
shortlists at most 25 recent exposures and 25 consumed sections before checking
admissions and ACLs. `complete` is always false: absence does not establish
nonexistence. `truncated` reports observed result/source limits.

The named world/character view graphs are generated exports of current SQL,
not independently persisted snapshots. Character export graph IDs include the
cognitive scope revision, so changed environment links produce a distinct view.
The per-source navigation triples above
are persisted by the existing background RDF writers. Future consumers must
continue to validate source access and revision; raw RDF is not an ACL or state
commit authority.
The generic AVAILABLE ACTIONS footer has been removed; registered backend action
capabilities are unchanged.

## HUD presentation

HUD v5 reads the character catalog during frame assembly, reusing the resolved
instance context. It replaces the old world-neighbor traversal: graph adjacency
no longer labels people or objects as present. The runtime location label remains
separate. The HUD never reads the unrestricted world catalog for character knowledge.

The prompt includes `KNOWN PLACES & ENTITIES (EVIDENCE; NOT A LIVE MAP)`:

```text
Place Kitchen @…: blue mug @… — in [reported; mentioned; @source…]
Person Bob @… [mentioned; @source…; current location unverified]
Person Obama @… [corpus exposure; @source…; current location unverified]
```

References are stable opaque hashes of existing IDs, not merged names. Full IDs,
source coordinates and membership evidence remain in `frame.entity_catalog`.
Denied, conditional/qualified and illustrative links retain their status and a
bounded source excerpt. Corpus exposure and acquisition remain distinct from
encounter records. These lines do not establish possession, present location,
availability, or successful actions.

Selection uses the former scene entity budget (approximately four characters per
token), includes at most eight complete rows, and prioritizes exact known current
location IDs and focus terms before source order. It does not resolve a reported
place to a runtime place by name. A profile with zero scene budget skips the read.
Catalog access has a 500 ms deadline and performs no LLM or Fuseki request. A
failed read is shown as unavailable, while an empty selection explicitly does
not establish absence. The HUD version change invalidates older prepared frames.

## Deployment

Stop old semantic/topology workers, run the normal migrations, then restart them.
`20261028_01_place_catalog_rdf.sql` resets only RDF receipts for existing built
entity and corpus projections. The normal bounded writers re-emit their graphs
without parsing text again. During replay, not-yet-ready projections are omitted
from the character catalog. No acquisitions, entities or grants are backfilled.

A useful Fuseki query, subject to the caller's source access checks:

```sparql
PREFIX catalog: <urn:aios:catalog#>
SELECT ?graph ?place ?member ?status WHERE {
  GRAPH ?graph {
    ?place a catalog:Place ; catalog:hasMembershipEvidence ?evidence .
    ?evidence catalog:member ?member ; catalog:membershipStatus ?status .
  }
} LIMIT 100
```
