# AIOS Knowledge Atlas — Stage 3: Progressive Research V1

See [Knowledge Atlas Stabilization V1](knowledge_atlas_stabilization_v1.md) for the source-node research-tool bridge, topic candidate hygiene and updated HUD boundary.

Stage 3 adds *persistent, character-scoped inquiry*, bounded continuation, and
deliberate source rematerialization on top of the Stage 1 Topic Atlas and
Stage 2 ACL-checked hybrid corpus discovery.

## Boundaries

A topic is worth researching without necessarily being true. A Qdrant hit,
Fuseki relationship, research question, dossier section, or displayed corpus
excerpt is not a character memory, world assertion or accepted proposition.

- PostgreSQL owns dossier identity, source provenance, explicit selection,
  work/lease budgets and idempotent completion receipts.
- Qdrant identifies corpus and topic candidate IDs through the warm Stage 2
  semantic query process. No additional embedding model is instantiated here.
- Fuseki is consulted via a bounded read-only traversal of the selected Topic
  Atlas named graph. Only neighbor UUIDs are used; candidate follow-ups must
  ALSO be linked in PostgreSQL to a passage actually exposed to this character.
  A failed Fuseki read falls back to the SQL topic mirror. There is no
  unrestricted graph crawl and no direct authority promotion.
- CharacterResearchService.search() is the **only** dossier candidate acquisition
  path. Its source results already passed per-character corpus SQL ACL including
  explicit denies/fanwork restrictions. Study calls
  CharacterResearchService.acquire() again and revalidates CURRENT permissions.
- consume_corpus_sections() is the existing cross-epistemic boundary. Its
  selected passage must match the digest saved when that dossier actually
  exposed it. Submitted/ingested indicates a source observation entered normal
  ingestion; it **does not** mean Integrity V4 or later semantic receipt approved
  the contents, and does not assert the character believes everything read.

## Database migration

Canonical incremental migration: migrations/current/20261004_17_progressive_research.sql

Tables:

* character_research_dossier — one scope-separated inquiry per instance/focus;
  open, paused and closed states; budgets for attempts, retained sections and
  deliberate source submissions.
* character_research_question — initial, requested and source-grounded
  topic-follow-up questions, each with its own stable key/status.
* character_research_step — request-ID keyed, leased, idempotent research cycle;
  at most one running step per dossier.
* character_research_source — source IDs, retrieval provenance, exact exposed
  source-text MD5 revision and submitted/discovered status.
* character_research_selection / character_research_materialization — explicit
  study decision and per-section durable ingestion receipt.

source_consumption gains an optional research_dedupe_key with a unique partial
(instance_id, research_dedupe_key) index. This lets a retry reuse the SAME
consumption_id and persist_external_observation dedupe key after an interrupted
study, rather than manufacturing another source event. Legacy calls with NULL
dedupe keys remain unchanged.

### Budgets and crash behavior

One research advance returns at most 8 authorized references. Default dossier
caps: 8 research attempts, 24 retained sections, 4 intentional
materializations. Hard maxima: 16, 48, and 8. One study operation submits at
most 2 distinct sections. Steps/selection leases are 2 minutes; a later request
can requeue an expired research question or resume the original selection.
Failed steps still count against the attempt cap, avoiding retry storms.
Repeat request_id returns the recorded receipt, not another ingestion event.

Only explicitly registered public catalogue relationships and provisional
source-associated topic edges feed follow-up candidates. No recursive research
in one call; each subsequent step must be selected by the operator or AIOS's
normal cognitive opportunity scheduler.

### Interaction with legacy learning

Automatic corpus *lookup* and reinforcement continue as before. To maintain a
clear Stage 3 decision boundary, automatic exposure-triggered source consumption
is now **off by default**. Source consumption happens via an explicit
research.study operation, the documented study endpoint, or existing explicit
acquisition API. Operators needing the former automatic scoring/acquisition
behavior can opt in with AIOS_LEGACY_AUTO_CORPUS_ACQUIRE=1.

This flag is independent of semantic Integrity V4 and belief admission; neither
research pathway bypasses those downstream checks.

## API examples

All examples are addressed to AIOS's existing API port and require real
instance/dossier/section UUIDs. Use a NEW request_id for a NEW operation and
repeat it when retrying a potentially interrupted operation.

Create or reuse a character's dossier:

    POST /agent/instance/{instance_id}/research
    {"question":"What is interesting about Digital World ecology?",
     "max_cycles":8,"max_sections":24,"max_materializations":4}

Inspect or list existing dossiers:

    GET /agent/instance/{instance_id}/research
    GET /agent/instance/{instance_id}/research/{dossier_id}

Advance ONE bounded cycle:

    POST /agent/instance/{instance_id}/research/{dossier_id}/advance
    {"request_id":"<unique-UUID>","include_fanwork":false}

Ask an additional question (still within the dossier's total question budget):

    POST /agent/instance/{instance_id}/research/{dossier_id}/question
    {"question":"Does this connect to Digimon evolution?"}

Deliberately study 1–2 previously exposed, currently authorized and unchanged
source sections:

    POST /agent/instance/{instance_id}/research/{dossier_id}/study
    {"request_id":"<unique-UUID>","section_ids":["<section-UUID>"]}

Pause, resume or close:

    POST /agent/instance/{instance_id}/research/{dossier_id}/status
    {"status":"paused"}
    {"status":"open"}
    {"status":"closed"}

Closed dossiers cannot reopen; a distinct focus question creates a new one.
Inspection only returns source excerpts if the CURRENT corpus ACL permits them
and the text digest still matches the exposure revision.

## Internal cognitive operations

- research.advance: general structured knowledge gaps create/reuse a dossier,
  run one hybrid corpus lookup and record compact IDs, counts and next topics.
- research.study: a separate proposal can offer an already exposed section for
  selected study. The operation itself revalidates access and content digest.
- inquiry.resolve: the established goal/inquiry planner is retained for
  goal-specific knowledge questions; Stage 3 does not commandeer that path.

A dossier's exhausted/closed/paused state suppresses repeated source-gap
proposals on subsequent conversation turns. Neither research.advance nor
research.study counts as goal satisfaction or creates a belief receipt.

## Verification and operation

Run the existing production migrator before restart (do not edit baseline):

    python -m aios_app.migrate

The canonical-cold-start CI job compiles new modules and executes real
PostgreSQL Stage 3 smoke on a disposable database. That smoke covers dossier
idempotence, wrong-instance rejection, ACL-restricted vector hits, topic
follow-ups, budgets, altered source text, revoked corpus access, deliberate
study and uniqueness of the source consumption key. Unit tests cover the
bounded Fuseki graph parser and receipt/digest consumption contracts. These
tests use mocked external networking; still verify actual live Qdrant/Fuseki
against disposable AIOS1 data before broader release.

Helpful PostgreSQL queries:

    SELECT instance_id,question,status,cycles_completed,max_cycles,max_sections,
           max_materializations,updated_at
    FROM aios.character_research_dossier
    ORDER BY updated_at DESC LIMIT 20;

    SELECT d.question,s.section_id,s.status,s.source_text_digest,
           m.consumption_id,m.status AS materialization
    FROM aios.character_research_dossier d
    JOIN aios.character_research_source s USING(dossier_id)
    LEFT JOIN aios.character_research_materialization m
      ON m.dossier_id=s.dossier_id AND m.section_id=s.section_id
    ORDER BY d.updated_at DESC LIMIT 30;

    SELECT dossier_id,query_text,status,source_count,newly_discovered,error
    FROM aios.character_research_step ORDER BY started_at DESC LIMIT 30;

If a source is revised after research, the old section cannot be studied until
a NEW authorized research exposure refreshes the dossier's saved text revision.
A closed/budget-exhausted dossier may be inspected, but cannot silently renew
its work cap. Research source records do not duplicate the actual source text;
the corpus remains cold and can be rematerialized on cue.
