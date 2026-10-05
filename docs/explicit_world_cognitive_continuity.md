# Explicit roleplay reality and cognitive continuity

This additive pass supersedes the implicit same-character-root session continuation
from `20260915_world_session_continuity.sql`. Never alter the checksums of old migrations.

- The DAG source coordinate remains (session, character, user, scope, timeline, head).
- Runtime worlds created without an explicit world choice are now keyed by
  character, client session and a stable SHA-256 digest of actor + source scope.
  A reused client session with a different persona no longer silently shares a world.
- No *new* automatic `continues` edges are produced, and the old tagged automatic
  edges are removed from `world_relation` and the materialized resolution cache.
  Explicitly authored history-continuation edges remain.
- `aios.cognitive_evidence_instances` preserves parent-instance lineage, then adds
  cross-instance evidence only from the current world or an *explicit* enabled
  `continues` history path. Different users within one old shared world cannot
  share private episodic cognition just because the session ID is equal.
  Explicit world continuation under character policy may cross actors; `user`
  policy still requires the same runtime actor. `isolated` remains instance lineage.
- Belief winners are reconciled after policy redefinition; the original acquisition,
  DAG and source receipts are not deleted.
- HUD's topology retrieval and flat fallback use the canonical cognitive instance
  set. The scorer uses the evidence instance's world rather than stamping every
  flat retrieval with the current runtime world's ID.
- Corpus auto lookup requires a focused direct request; searching a library *in a
  story* is not a cold-corpus query. Low-similarity vector hints are discarded.
- Non-durable errands and bare scene-location commitments are excluded from the
  managed-goal path; demonstrated stale goals are cancelled using their source
  admission receipts. Current-message external narration remains in RECENT EVENTS
  rather than being repeated as character-owned fast episodic memory.

## Important compatibility note

New automatic worlds have a changed deterministic key. A previously active legacy
world/instance is preserved for audit; after upgrade, reactivate the client so a
new scope-qualified world can be selected. An intentionally shared world can still
be selected explicitly with `world_id` or `world_key`. An explicit shared world is
*not* by itself a grant to copy another user's private memory.

Resume within an existing exact runtime identity is supported. Future explicit
fork-point cutoffs, multi-parent merge conflict resolution and formal episode
closed/reopened state machines are *not* implemented by this safety patch.
A `continues` edge asserts compatible prior history; create it only deliberately.

Verify the actual scope with:

```sql
SELECT e.instance_id,e.depth,ci.meta->>'runtime_user_name' AS runtime_user
FROM aios.cognitive_evidence_instances('58c7813a-b8f6-40ab-bc42-b7a79291b0db'::uuid) e
JOIN aios.character_instance ci ON ci.instance_id=e.instance_id
ORDER BY e.depth;

SELECT world_id,source_world_id,relation,domains,meta
FROM aios.world_relation WHERE enabled;
```
