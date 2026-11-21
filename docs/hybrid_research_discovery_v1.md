# Hybrid research: discovery bridge v1 (feature branch)

This pass connects AIOS-Client's **existing** opt-in SearXNG, Calibre OPDS, and local-document
research tools to AIOS's **existing** instance-scoped progressive research dossiers.

It does **not** import documents, promote claims to facts, create character memories, or
automatically approve source access. It is intended to validate evidence provenance and
the worker-to-dossier authorization boundary before a second corpus-acquisition pass.

## Deployment

1. Check out `feature/hybrid-research-discovery-v1` in both AIOS and AIOS_Client.
2. Run the canonical AIOS migrator (`python -m aios_app.migrate`), then restart AIOS.
3. Configure and grant AIOS-Client research sources using `docs/research.md`.
   SearXNG JSON search and Calibre anonymous OPDS are already supported.
4. Start an AIOS research dossier using
   `POST /agent/instance/{instance_id}/research` with a concrete question.
   Retain its returned `dossier_id`.
5. Authorize a normal delegated workspace v3 assignment to the same character.
   In a research-only client profile set `tool_mode: "research"`.
   Give the worker the dossier ID in its objective. The worker can search,
   read and nominate evidence with `research_nominate` when the server advertises
   `research-discovery-v1`.

The client sends a leased `POST /workspace-client/v3/{binding_id}/runs/{run_id}/research/nominate`
request using its normal worker-key transport with `claim_token`. It never needs
the operator review key. The endpoint checks the live run lease, owner instance
(or explicit origin instance for delegated work), open dossier, protocol v3, and
idempotent evidence identity. An interrupted run cannot submit new nominations.

An example client-side tool argument is:

```json
{
  "evidence_id": "aaaaaaaaaaaaaaaaaaaaaaaa",
  "dossier_id": "11111111-1111-4111-8111-111111111111",
  "reason": "Contains a source-grounded observation relevant to the open question"
}
```

These IDs are illustrative only. The actual evidence_id must correspond to a
source the client actually read in the same work run, and the dossier must exist
and belong to the originating character. The client verifies the run-scoped
retrieval receipt; the server sees worker-attested provenance, **not remote
attestation** and not independent verification of the claim.

## State boundaries

* `research_external_nomination` stores a source reference, hash, title,
  retrieval metadata, reason, dossier, worker run and binding. Its default state is
  `nominated`. No source text, whole books, command logs or private filesystem
  paths are copied to AIOS.
* `character_research_source` remains restricted to actual corpus passages
  already exposed through authorized AIOS research.
* `corpus_document` and `corpus_section` are not modified by nomination.
* `consume_corpus_sections()`, belief admission and /char memory are not invoked.
* Source sharing is **not** automatically granted, even for public URLs.
* The existing result report still supports short cited findings independently.

Inspect nominations on a trusted AIOS PostgreSQL connection:

```sql
SELECT nomination_id, dossier_id, source_id, kind, title,
       document_sha256, status, created_at
FROM aios.research_external_nomination
ORDER BY created_at DESC LIMIT 30;
```

## Scope for the next coding pass

The next pass needs operator-defined per-source import policy, source-identity
deduplication (across URLs and revisions), a bounded content-staging interface,
and an idempotent ingestion worker that calls the existing `import_corpus_document()`.
An imported document must become eligible for `ProgressiveResearchService.advance()`
and only then for separately authorized `research.study`; a nominated source
alone never becomes an acquired character proposition.

Run `pytest tests/test_research_discovery_bridge.py` in AIOS and
`pytest tests/test_research_bridge.py` in AIOS_Client, plus the existing research
and workspace suites. The feature branch has not been validated against a live
AIOS/PostgreSQL and Calibre/SearXNG installation.
