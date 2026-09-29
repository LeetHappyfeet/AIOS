# Structured epistemic retrieval

AIOS treats retrieval as query planning over epistemic stores rather than as one vector-search operation.

## Ownership boundary

`/char` is character-relative experience, memory and belief. `/world` is admitted viewpoint-independent world state/evidence. Retrieval never silently falls from `/char` into `/world`. `epistemic.compare` executes the two sides independently and compares semantic atoms without merging ownership.

## Execution roles

- Semantic/vector search discovers coordinates when durable coordinates are not already available.
- Character cognitive subjects and their evidence provide the first exact coordinate lookup for structured `/char` questions.
- PostgreSQL semantic topology answers bounded, directed relationship queries.
- PostgreSQL acquisition, authority and belief tables answer evidence/provenance questions.
- The DAG answers temporal before/after questions.
- RDF is a derived graph projection. It may later accelerate or express typed graph paths, but it is not an authority boundary and structured results must retain SQL coordinates for validation.

Once a structured query has proposition coordinates, it does not return to vector similarity to determine the answer.

## Agent surface

`knowledge.lookup` supports `search`, `evidence`, `relation`, and `history`. `world.lookup` supports `search`, `evidence`, and `relation`. `epistemic.compare` explicitly compares character knowledge with independently admitted world state.

Relationship traversal is limited to two hops. The caller may constrain edge types and direction. The agent never receives arbitrary SQL or SPARQL execution.

`unverified` in an epistemic comparison means that no admitted `/world` assertion for the same semantic atom was found. It does not mean the character belief is false.
