"""Advisory Topic Atlas collection: mentions are not claims, evidence or access grants.

Reads existing semantic output and structured corpus metadata. All writes are
source-revision keyed and scoped; no LLM, vector search or ontology promotion.
"""
from __future__ import annotations

import hashlib
import re
from itertools import combinations
from typing import Any

POLICY_VERSION = "topic-atlas-discovery-v1"
_WS = re.compile(r"\s+")
_OPAQUE = re.compile(r"(?i)(?:[a-f0-9]{32,64}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}|topic-v\d+:[a-f0-9]+)")
_TRAILING = re.compile(r"(?i)\b(?:and|or|of|the|with|for|to|in|at|a|an)$")
_BAD = frozenset({
    "i", "me", "my", "we", "us", "our", "you", "your", "he", "his", "she",
    "her", "they", "them", "it", "its", "someone", "something", "unknown",
    "none", "null", "other", "_", "*",
})


def normalize_label(value: Any) -> str | None:
    if value is None:
        return None
    label = _WS.sub(" ", str(value).strip().replace("_", " "))
    folded = label.casefold()
    if (len(label) < 2 or len(label) > 112 or len(label.split()) > 10
            or folded in _BAD or "://" in label or "@" in label
            or not any(ch.isalpha() for ch in label)
            or any(ord(ch) < 32 for ch in label)
            or _OPAQUE.fullmatch(label.strip()) is not None
            or _TRAILING.search(folded) is not None):
        return None
    return folded


def topic_identity(namespace: str, kind: str, identifier: str) -> str:
    normalized = normalize_label(identifier)
    if normalized is None:
        raise ValueError("topic identity needs a stable, non-placeholder label")
    material = f"{namespace}\x1f{kind}\x1f{normalized}".encode("utf-8")
    return "topic-v1:" + hashlib.sha256(material).hexdigest()


def topic_scope(*, character_id: str | None = None,
                world_id: Any = None, domain_key: str | None = None) -> tuple[str, str, str | None]:
    if character_id:
        return f"char:{character_id}", "private", character_id
    if world_id:
        return f"world:{world_id}", "catalog", None
    if domain_key:
        return f"catalog:{domain_key}", "catalog", None
    # Unknown provenance must never enter the publicly searchable catalogue.
    return "unresolved-source", "private", None


async def _upsert_topic(con, *, namespace: str, visibility: str, owner: str | None,
                        kind: str, label: str, identifier: str | None = None,
                        registered: bool = False):
    # Internal entity keys can be opaque while a frame's resolved name is
    # readable. Preserve the key in its originating frame; use a linguistic
    # topic identity rather than embedding/deduplicating an internal hash.
    canonical = normalize_label(identifier) or normalize_label(label)
    display = _WS.sub(" ", str(label).strip())
    if canonical is None or normalize_label(display) is None:
        return None
    key = topic_identity(namespace, kind, canonical)
    # This catalogue has TWO independent unique constraints. Historical
    # collectors sometimes used an opaque entity identifier as the normalized
    # label; newer source labels can therefore match a different existing row
    # by natural identity or collide with a legacy topic_key. A one-target
    # UPSERT merely shifts UniqueViolation between those constraints.
    inserted = await con.fetchrow(
        """INSERT INTO aios.knowledge_topic (
             topic_key,namespace,topic_kind,normalized_label,display_label,
             status,visibility,owner_character_id)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
           ON CONFLICT DO NOTHING RETURNING topic_id""",
        key, namespace, kind, canonical, display,
        "registered" if registered else "candidate", visibility, owner,
    )
    if inserted:
        topic_id = inserted["topic_id"]
    else:
        # _collect_row holds the global topic-discovery advisory transaction
        # lock. Lock the existing identities and select the NATURAL topic when
        # a legacy key and normalized label resolve to different topic IDs.
        matches = await con.fetch(
            """SELECT topic_id,topic_key,namespace,topic_kind,
                      normalized_label,display_label,status
               FROM aios.knowledge_topic
               WHERE topic_key=$1 OR (
                   namespace=$2 AND topic_kind=$3 AND normalized_label=$4)
               FOR UPDATE""",
            key, namespace, kind, canonical,
        )
        by_natural = next(
            (r for r in matches if r["namespace"] == namespace
             and r["topic_kind"] == kind
             and r["normalized_label"] == canonical), None)
        by_key = next((r for r in matches if r["topic_key"] == key), None)
        existing = by_natural or by_key
        if existing is None:
            # A non-cooperating writer changed the row between the initial
            # insert and our locked lookup. Retry the source transaction later.
            raise RuntimeError("topic identity changed concurrently during collection")
        if by_natural is not None and by_key is not None and (
                by_natural["topic_id"] != by_key["topic_id"]):
            # Never merge independent historical source/mention foreign keys.
            # Select the already canonical natural row; preserve the legacy
            # key-holder for the bounded source-reconciliation lifecycle.
            import logging
            logging.getLogger(__name__).warning(
                "Atlas split legacy identities namespace=%s kind=%s label=%s "
                "canonical_topic_id=%s legacy_key_topic_id=%s",
                namespace, kind, canonical,
                by_natural["topic_id"], by_key["topic_id"],
            )
        topic_id = existing["topic_id"]
        repair_label = (by_natural is None and by_key is not None
                        and existing["namespace"] == namespace
                        and existing["topic_kind"] == kind
                        and existing["normalized_label"] != canonical)
        if by_natural is None and by_key is not None and not repair_label and (
                existing["namespace"] != namespace or existing["topic_kind"] != kind):
            # A corrupted key pointing outside its hashed scope cannot
            # authorize cross-character topic identity.
            raise ValueError("topic key conflicts with a different scope or topic kind")
        if existing["status"] == "retired":
            next_status = "registered" if registered else "candidate"
        elif registered and existing["status"] == "candidate":
            next_status = "registered"
        else:
            next_status = existing["status"]
        # Explicit registrations may refresh a display label. Candidate
        # replays otherwise leave existing human-readable presentation alone,
        # except when repairing the same hashed key's stale natural label.
        next_display = (
            display if registered or repair_label else existing["display_label"]
        )
        revision_changed = (
            repair_label
            or existing["status"] == "retired"
            or (registered and (
                existing["status"] == "candidate"
                or existing["display_label"] != next_display))
        )
        await con.execute(
            """UPDATE aios.knowledge_topic
               SET normalized_label=CASE WHEN $2 THEN $3 ELSE normalized_label END,
                   display_label=$4,status=$5,
                   vector_revision=vector_revision+CASE WHEN $6 THEN 1 ELSE 0 END,
                   graph_revision=graph_revision+CASE WHEN $6 THEN 1 ELSE 0 END,
                   updated_at=CASE WHEN $6 THEN now() ELSE updated_at END
               WHERE topic_id=$1""",
            topic_id, repair_label, canonical, next_display,
            next_status, revision_changed,
        )
    # The identifier is a lookup aid, not a claim that two external entities are identical.
    alias = normalize_label(display)
    if alias != canonical:
        inserted = await con.fetchrow(
            """INSERT INTO aios.knowledge_topic_alias
               (topic_id,normalized_alias,display_alias,alias_kind,source_kind,source_key)
               VALUES ($1,$2,$3,'observed','topic_collector',$4)
               ON CONFLICT DO NOTHING RETURNING topic_id""",
            topic_id, alias, display, key,
        )
        if inserted:
            await con.execute(
                """UPDATE aios.knowledge_topic
                   SET vector_revision=vector_revision+1,graph_revision=graph_revision+1,
                       updated_at=now() WHERE topic_id=$1""", topic_id)
    return topic_id


# Each source query checks a source-specific revision receipt. Corpus entries are
# observations of headings/facets only; document body is NOT atomized here.
_SOURCE_QUERIES = (
    ("knowledge_domain", """
      WITH s AS (
        SELECT d.domain_id::text source_key, d.domain_id::text origin_key,
               md5(d.domain_key || '|' || d.display_name || '|' || d.enabled::text ||
                   '|' || COALESCE(d.parent_domain_id::text,'')) source_revision,
               d.domain_key identifier, d.display_name label, d.domain_key,
               NULL::uuid document_id,NULL::uuid section_id,
               NULL::uuid claim_id,NULL::uuid instance_id,
               NULL::text character_id,NULL::uuid world_id,
               'domain'::text topic_kind
        FROM aios.knowledge_domain d WHERE d.enabled
      ) SELECT s.* FROM s WHERE NOT EXISTS
          (SELECT 1 FROM aios.knowledge_topic_discovery_receipt r
           WHERE r.source_kind='knowledge_domain' AND r.source_key=s.source_key
             AND r.source_revision=s.source_revision)
        ORDER BY source_key LIMIT $1
    """),
    ("corpus_heading", """
      WITH s AS (
        SELECT cs.section_id::text source_key, cs.document_id::text origin_key,
               md5(cs.heading || '|' || cs.content_hash || '|' ||
                   COALESCE((SELECT min(knowledge_domain) FROM aios.corpus_document_domain
                             WHERE document_id=cs.document_id),'unclassified')) source_revision,
               cs.heading identifier,cs.heading label,
               COALESCE((SELECT min(knowledge_domain) FROM aios.corpus_document_domain
                         WHERE document_id=cs.document_id),'unclassified') domain_key,
               cs.document_id,cs.section_id,
               NULL::uuid claim_id,NULL::uuid instance_id,
               NULL::text character_id,NULL::uuid world_id,
               'concept'::text topic_kind
        FROM aios.corpus_section cs
        WHERE NULLIF(btrim(cs.heading),'') IS NOT NULL
      ) SELECT s.* FROM s WHERE NOT EXISTS
          (SELECT 1 FROM aios.knowledge_topic_discovery_receipt r
           WHERE r.source_kind='corpus_heading' AND r.source_key=s.source_key
             AND r.source_revision=s.source_revision)
        ORDER BY source_key LIMIT $1
    """),
    ("corpus_facet", """
      WITH s AS (
        SELECT f.document_id::text || ':' || f.facet_type || ':' || f.facet_value source_key,
               f.document_id::text origin_key,
               md5(f.facet_type || '|' || f.facet_value || '|' ||
                  COALESCE((SELECT min(knowledge_domain) FROM aios.corpus_document_domain
                            WHERE document_id=f.document_id),'unclassified')) source_revision,
               f.facet_value identifier,f.facet_value label,
               COALESCE((SELECT min(knowledge_domain) FROM aios.corpus_document_domain
                         WHERE document_id=f.document_id),'unclassified') domain_key,
               f.document_id,NULL::uuid section_id,
               NULL::uuid claim_id,NULL::uuid instance_id,
               NULL::text character_id,NULL::uuid world_id,
               CASE WHEN f.facet_type IN ('character','species','subject') THEN 'entity'
                    ELSE 'concept' END topic_kind
        FROM aios.corpus_document_facet f
        WHERE f.facet_type IN ('subject','character','species','concept','topic',
                               'genre','fandom','franchise','universe','domain')
      ) SELECT s.* FROM s WHERE NOT EXISTS
          (SELECT 1 FROM aios.knowledge_topic_discovery_receipt r
           WHERE r.source_kind='corpus_facet' AND r.source_key=s.source_key
             AND r.source_revision=s.source_revision)
        ORDER BY source_key LIMIT $1
    """),
    ("claim_candidate", """
      WITH s AS (
        SELECT cc.claim_id::text source_key,cc.claim_id::text origin_key,
               md5(concat_ws('|',cc.subject,cc.object,
                   c.character_instance_id::text,c.origin_character_id,c.world_id::text)) source_revision,
               cc.subject subject_label,cc.object object_label,
               cc.claim_id,c.character_instance_id instance_id,
               c.origin_character_id character_id,c.world_id,
               NULL::text domain_key,NULL::uuid document_id,NULL::uuid section_id
        FROM aios.claim_candidate cc
        JOIN aios.claim_context_resolution c ON c.claim_id=cc.claim_id
        WHERE NULLIF(btrim(cc.subject),'') IS NOT NULL
           OR NULLIF(btrim(cc.object),'') IS NOT NULL
      ) SELECT s.* FROM s WHERE NOT EXISTS
          (SELECT 1 FROM aios.knowledge_topic_discovery_receipt r
           WHERE r.source_kind='claim_candidate' AND r.source_key=s.source_key
             AND r.source_revision=s.source_revision)
        ORDER BY source_key LIMIT $1
    """),
    ("semantic_frame", """
      WITH s AS (
        SELECT f.frame_id::text source_key, f.claim_id::text origin_key,
               md5(concat_ws('|', f.subject_entity_key,f.object_entity_key,
                     f.resolved_subject,f.resolved_object,
                     c.character_instance_id::text,c.origin_character_id,c.world_id::text)) source_revision,
               f.subject_entity_key subject_key,f.object_entity_key object_key,
               f.resolved_subject subject_label,f.resolved_object object_label,
               f.claim_id,c.character_instance_id instance_id,
               c.origin_character_id character_id,c.world_id,
               NULL::text domain_key,NULL::uuid document_id,NULL::uuid section_id
        FROM aios.claim_semantic_frame f
        LEFT JOIN aios.claim_context_resolution c ON c.claim_id=f.claim_id
        WHERE f.decomposer_version='semantic-frame-v2'
          AND (NULLIF(f.subject_entity_key,'') IS NOT NULL
               OR NULLIF(f.object_entity_key,'') IS NOT NULL)
      ) SELECT s.* FROM s WHERE NOT EXISTS
          (SELECT 1 FROM aios.knowledge_topic_discovery_receipt r
           WHERE r.source_kind='semantic_frame' AND r.source_key=s.source_key
             AND r.source_revision=s.source_revision)
        ORDER BY source_key LIMIT $1
    """),
    ("proposition_occurrence", """
      WITH s AS (
        SELECT o.observation_id::text source_key,o.claim_id::text origin_key,
               md5(concat_ws('|',p.topic_key,p.proposition_id::text,
                   c.character_instance_id::text,c.origin_character_id,c.world_id::text)) source_revision,
               p.topic_key identifier,p.topic_key label,
               o.claim_id,c.character_instance_id instance_id,
               c.origin_character_id character_id,c.world_id,
               NULL::text domain_key,NULL::uuid document_id,NULL::uuid section_id,
               'concept'::text topic_kind
        FROM aios.observation o JOIN aios.proposition p
          ON p.proposition_id=o.proposition_id
        LEFT JOIN aios.claim_context_resolution c ON c.claim_id=o.claim_id
        WHERE NULLIF(btrim(p.topic_key),'') IS NOT NULL
      ) SELECT s.* FROM s WHERE NOT EXISTS
          (SELECT 1 FROM aios.knowledge_topic_discovery_receipt r
           WHERE r.source_kind='proposition_occurrence' AND r.source_key=s.source_key
             AND r.source_revision=s.source_revision)
        ORDER BY source_key LIMIT $1
    """),
)


async def _collect_row(db, source_kind: str, row: Any) -> int:
    row = dict(row)
    namespace, visibility, owner = (
        ("catalog", "catalog", None)
        if source_kind == "knowledge_domain"
        else topic_scope(
            character_id=row.get("character_id") if row.get("instance_id") else None,
            world_id=row.get("world_id"),
            domain_key=row.get("domain_key"),
        )
    )
    candidates: list[tuple[str, str, str | None]] = []
    if source_kind == "claim_candidate":
        # Permissive, low-trust surface inventory. These are NEVER factual claims.
        for label in (row["subject_label"], row["object_label"]):
            if normalize_label(label) and len(str(label).split()) <= 4:
                candidates.append(("concept", label, None))
    elif source_kind == "semantic_frame":
        for identity, label in (
            (row["subject_key"], row["subject_label"]),
            (row["object_key"], row["object_label"]),
        ):
            if normalize_label(label) or normalize_label(identity):
                candidates.append(("entity", label if normalize_label(label) else identity,
                                   identity if normalize_label(identity) else None))
    else:
        if normalize_label(row.get("label")):
            candidates.append((row["topic_kind"], row["label"], row["identifier"]))
    # No second entity extraction, no sentence-level claim generation.
    async with db.connection() as con:
        async with con.transaction():
            await con.execute("SELECT pg_advisory_xact_lock(hashtext('aios.topic.discovery.v1'))")
            receipt = await con.fetchrow(
                """SELECT source_revision FROM aios.knowledge_topic_discovery_receipt
                   WHERE source_kind=$1 AND source_key=$2""",
                source_kind, row["source_key"])
            if receipt and receipt["source_revision"] == row["source_revision"]:
                return 0
            old = await con.fetch(
                """SELECT DISTINCT topic_id FROM aios.knowledge_topic_mention
                   WHERE source_kind=$1 AND source_key=$2
                   UNION SELECT source_topic_id FROM aios.knowledge_topic_relation
                   WHERE source_kind=$1 AND source_key=$2
                   UNION SELECT target_topic_id FROM aios.knowledge_topic_relation
                   WHERE source_kind=$1 AND source_key=$2""",
                source_kind, row["source_key"])
            old_mentions = {r["topic_id"] for r in await con.fetch(
                """SELECT topic_id FROM aios.knowledge_topic_mention
                   WHERE source_kind=$1 AND source_key=$2""", source_kind,row["source_key"])}
            old_edges = {(r["source_topic_id"],r["target_topic_id"]) for r in await con.fetch(
                """SELECT source_topic_id,target_topic_id
                   FROM aios.knowledge_topic_relation
                   WHERE source_kind=$1 AND source_key=$2""",source_kind,row["source_key"])}
            old_links = {(r["topic_id"],r["document_id"],r["section_id"])
                         for r in await con.fetch(
                """SELECT topic_id,document_id,section_id
                   FROM aios.knowledge_topic_source
                   WHERE link_kind=$1 AND source_key=$2""",source_kind,row["source_key"])}
            await con.execute(
                "DELETE FROM aios.knowledge_topic_mention WHERE source_kind=$1 AND source_key=$2",
                source_kind, row["source_key"])
            await con.execute(
                "DELETE FROM aios.knowledge_topic_relation WHERE source_kind=$1 AND source_key=$2",
                source_kind, row["source_key"])
            await con.execute(
                "DELETE FROM aios.knowledge_topic_source WHERE link_kind=$1 AND source_key=$2",
                source_kind, row["source_key"])
            old_ids = [r["topic_id"] for r in old]
            topic_ids = []
            for kind, label, identity in candidates:
                topic_id = await _upsert_topic(
                    con, namespace=namespace, visibility=visibility, owner=owner,
                    kind=kind, label=label, identifier=identity,
                    registered=(source_kind == "knowledge_domain"))
                if not topic_id or topic_id in topic_ids:
                    continue
                topic_ids.append(topic_id)
                await con.execute(
                    """INSERT INTO aios.knowledge_topic_mention
                       (topic_id,source_kind,source_key,origin_key,source_revision,
                        claim_id,instance_id,character_id,world_id,meta)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb)""",
                    topic_id, source_kind,row["source_key"],row["origin_key"],
                    row["source_revision"],row.get("claim_id"),row.get("instance_id"),
                    row.get("character_id"),row.get("world_id"),"{}")
                if row.get("document_id"):
                    await con.execute(
                        """INSERT INTO aios.knowledge_topic_source
                           (topic_id,document_id,section_id,link_kind,source_key,source_revision)
                           VALUES ($1,$2,$3,$4,$5,$6)
                           ON CONFLICT (topic_id,link_kind,source_key)
                           DO UPDATE SET source_revision=EXCLUDED.source_revision""",
                        topic_id,row["document_id"],row.get("section_id"),
                        source_kind,row["source_key"],row["source_revision"])
            new_edges=set(combinations(sorted(topic_ids, key=str),2))
            for a, b in new_edges:
                await con.execute(
                    """INSERT INTO aios.knowledge_topic_relation
                       (source_topic_id,target_topic_id,relation_kind,status,
                        source_kind,source_key,source_revision)
                       VALUES ($1,$2,'associated','candidate',$3,$4,$5)
                       ON CONFLICT DO NOTHING""",
                    a,b,source_kind,row["source_key"],row["source_revision"])
            # Graph contains aggregate navigation and public source counts,
            # not raw mention/source revision. A refresh with unchanged
            # effective topic/edge membership must not invalidate Fuseki.
            changed=set(old_mentions)^set(topic_ids)
            for a,b in old_edges^new_edges:
                changed.update((a,b))
            new_links={(topic_id,row["document_id"],row.get("section_id"))
                       for topic_id in topic_ids} if row.get("document_id") else set()
            if old_links != new_links:
                changed.update(link[0] for link in old_links^new_links)
            if changed:
                await con.execute(
                    """UPDATE aios.knowledge_topic SET graph_revision=graph_revision+1,
                       updated_at=now() WHERE topic_id=ANY($1::uuid[])""",list(changed))
            affected = set(old_ids) | set(topic_ids)
            for topic_id in affected:
                # Source/claim dedup: frame and observation for one claim count once.
                chars = await con.fetch(
                    """SELECT DISTINCT character_id FROM aios.knowledge_topic_mention
                       WHERE topic_id=$1 AND character_id IS NOT NULL
                       UNION SELECT character_id FROM aios.knowledge_topic_interest
                       WHERE topic_id=$1""",topic_id)
                for item in chars:
                    await con.execute(
                        """INSERT INTO aios.knowledge_topic_interest
                           (topic_id,character_id,distinct_origin_count)
                           VALUES ($1,$2,(SELECT count(DISTINCT origin_key)
                                 FROM aios.knowledge_topic_mention WHERE topic_id=$1 AND character_id=$2))
                           ON CONFLICT(topic_id,character_id) DO UPDATE
                           SET distinct_origin_count=EXCLUDED.distinct_origin_count,
                               last_seen_at=now()""",topic_id,item["character_id"])
            if affected:
                # A revised source must not leave a now-orphaned candidate
                # discoverable as if it were still mentioned.
                await con.execute(
                    """UPDATE aios.knowledge_topic t
                       SET status='retired',vector_revision=vector_revision+1,
                           graph_revision=graph_revision+1,updated_at=now()
                       WHERE t.topic_id=ANY($1::uuid[])
                         AND t.status IN ('candidate','registered')
                         AND NOT EXISTS (SELECT 1 FROM aios.knowledge_topic_mention m
                                         WHERE m.topic_id=t.topic_id)""",
                    list(affected))
            await con.execute(
                """INSERT INTO aios.knowledge_topic_discovery_receipt
                   (source_kind,source_key,source_revision,outcome)
                   VALUES ($1,$2,$3,$4)
                   ON CONFLICT(source_kind,source_key) DO UPDATE
                   SET source_revision=EXCLUDED.source_revision,outcome=EXCLUDED.outcome,
                       processed_at=now()""",
                source_kind,row["source_key"],row["source_revision"],
                "collected" if topic_ids else "skipped")
    return len(topic_ids)


async def retire_missing_sources_once(db, *, limit: int = 8) -> int:
    """Tombstone changed/removed source coordinates, not the original source data."""
    rows = await db.fetch(
        """SELECT r.source_kind,r.source_key
           FROM aios.knowledge_topic_discovery_receipt r
           WHERE r.source_revision<>'deleted' AND (
             (r.source_kind='knowledge_domain' AND NOT EXISTS (
                SELECT 1 FROM aios.knowledge_domain d
                WHERE d.domain_id::text=r.source_key AND d.enabled))
             OR (r.source_kind='corpus_heading' AND NOT EXISTS (
                SELECT 1 FROM aios.corpus_section cs
                WHERE cs.section_id::text=r.source_key
                  AND NULLIF(btrim(cs.heading),'') IS NOT NULL))
             OR (r.source_kind='corpus_facet' AND NOT EXISTS (
                SELECT 1 FROM aios.corpus_document_facet f
                WHERE f.document_id::text||':'||f.facet_type||':'||f.facet_value=r.source_key))
             OR (r.source_kind='claim_candidate' AND NOT EXISTS (
                SELECT 1 FROM aios.claim_candidate cc
                JOIN aios.claim_context_resolution c ON c.claim_id=cc.claim_id
                WHERE cc.claim_id::text=r.source_key
                  AND (NULLIF(btrim(cc.subject),'') IS NOT NULL
                    OR NULLIF(btrim(cc.object),'') IS NOT NULL)))
             OR (r.source_kind='semantic_frame' AND NOT EXISTS (
                SELECT 1 FROM aios.claim_semantic_frame f
                WHERE f.frame_id::text=r.source_key
                  AND f.decomposer_version='semantic-frame-v2'
                  AND (NULLIF(f.subject_entity_key,'') IS NOT NULL
                    OR NULLIF(f.object_entity_key,'') IS NOT NULL)))
             OR (r.source_kind='proposition_occurrence' AND NOT EXISTS (
                SELECT 1 FROM aios.observation o JOIN aios.proposition p
                  ON p.proposition_id=o.proposition_id
                WHERE o.observation_id::text=r.source_key
                  AND NULLIF(btrim(p.topic_key),'') IS NOT NULL))
           ) ORDER BY r.processed_at,r.source_kind,r.source_key LIMIT $1""",
        max(1,min(int(limit),32)))
    for row in rows:
        # The normal transactional revision path retracts links, updates graph
        # revisions and retires orphaned topics. A tombstone stops repeat scans.
        await _collect_row(db,row["source_kind"],{
            "source_key":row["source_key"],"source_revision":"deleted",
            "origin_key":row["source_key"],"label":None,"identifier":None,
            "domain_key":None,"document_id":None,"section_id":None,
            "character_id":None,"world_id":None,"instance_id":None,
            "topic_kind":"concept",
            "subject_key":None,"object_key":None,
            "subject_label":None,"object_label":None,
        })
    return len(rows)


async def collect_topics_once(db, *, limit: int = 16) -> dict[str, int]:
    """Bounded, idempotent discovery. Every lane advances independently."""
    budget = max(1, min(int(limit), 64))
    counts = {}
    for source_kind, sql in _SOURCE_QUERIES:
        rows = await db.fetch(sql, budget)
        count = 0
        for row in rows:
            count += await _collect_row(db, source_kind, row)
        counts[source_kind] = count
    counts["retired_sources"] = await retire_missing_sources_once(db, limit=budget)
    counts["retired_domain_links"] = await retire_stale_domain_links_once(db, limit=budget)
    counts["domain_hierarchy"] = await reconcile_domain_hierarchy_once(db, limit=budget)
    counts["catalog_links"] = await reconcile_catalog_domain_links_once(db, limit=budget)
    return counts


async def retire_stale_domain_links_once(db, *, limit: int = 16) -> int:
    """Withdraw catalogue navigation after domain removal/reparenting."""
    rows = await db.fetch(
        """SELECT r.source_topic_id,r.target_topic_id,r.relation_kind,
                  r.source_kind,r.source_key
           FROM aios.knowledge_topic_relation r
           WHERE (r.source_kind='domain_hierarchy' AND NOT EXISTS (
             SELECT 1 FROM aios.knowledge_domain d
             JOIN aios.knowledge_domain parent ON parent.domain_id=d.parent_domain_id
               AND parent.enabled
             JOIN aios.knowledge_topic_mention dm
               ON dm.source_kind='knowledge_domain' AND dm.source_key=d.domain_id::text
               AND dm.topic_id=r.source_topic_id
             JOIN aios.knowledge_topic_mention pm
               ON pm.source_kind='knowledge_domain' AND pm.source_key=parent.domain_id::text
               AND pm.topic_id=r.target_topic_id
             WHERE d.enabled AND d.domain_id::text=r.source_key))
           OR (r.source_kind IN ('corpus_heading','corpus_facet','vector_candidate')
               AND r.relation_kind='associated' AND NOT EXISTS (
             SELECT 1 FROM aios.knowledge_topic_source s
             JOIN aios.knowledge_topic t ON t.topic_id=s.topic_id
             JOIN aios.corpus_document_domain cdd ON cdd.document_id=s.document_id
               AND t.namespace='catalog:'||cdd.knowledge_domain
             JOIN aios.knowledge_domain d ON d.domain_key=cdd.knowledge_domain
               AND d.enabled
             JOIN aios.knowledge_topic_mention pm
               ON pm.source_kind='knowledge_domain' AND pm.source_key=d.domain_id::text
               AND pm.topic_id=r.target_topic_id
             WHERE s.topic_id=r.source_topic_id AND s.link_kind=r.source_kind
               AND s.source_key=r.source_key))
           ORDER BY r.created_at,r.source_topic_id LIMIT $1""",
        max(1,min(int(limit),32)))
    removed = 0
    for row in rows:
        async with db.connection() as con:
            async with con.transaction():
                changed = await con.fetchrow(
                    """DELETE FROM aios.knowledge_topic_relation
                       WHERE source_topic_id=$1 AND target_topic_id=$2
                         AND relation_kind=$3 AND source_kind=$4 AND source_key=$5
                       RETURNING source_topic_id""",
                    row["source_topic_id"],row["target_topic_id"],
                    row["relation_kind"],row["source_kind"],row["source_key"])
                if changed:
                    await con.execute(
                        """UPDATE aios.knowledge_topic
                           SET graph_revision=graph_revision+1,updated_at=now()
                           WHERE topic_id=ANY($1::uuid[])""",
                        [row["source_topic_id"],row["target_topic_id"]])
                    removed += 1
    return removed


async def reconcile_domain_hierarchy_once(db, *, limit: int = 16) -> int:
    """Only explicitly registered domain parentage can create a verified edge."""
    rows = await db.fetch(
        """SELECT child_topic.topic_id AS child, parent_topic.topic_id AS parent,
                  d.domain_id::text source_key
           FROM aios.knowledge_domain d
           JOIN aios.knowledge_domain parent ON parent.domain_id=d.parent_domain_id
           JOIN aios.knowledge_topic_mention cm ON cm.source_kind='knowledge_domain'
                AND cm.source_key=d.domain_id::text
           JOIN aios.knowledge_topic child_topic ON child_topic.topic_id=cm.topic_id
           JOIN aios.knowledge_topic_mention pm ON pm.source_kind='knowledge_domain'
                AND pm.source_key=parent.domain_id::text
           JOIN aios.knowledge_topic parent_topic ON parent_topic.topic_id=pm.topic_id
           WHERE NOT EXISTS (
             SELECT 1 FROM aios.knowledge_topic_relation r
             WHERE r.source_topic_id=child_topic.topic_id
               AND r.target_topic_id=parent_topic.topic_id
               AND r.relation_kind='broader' AND r.source_kind='domain_hierarchy'
               AND r.source_key=d.domain_id::text)
           ORDER BY d.domain_key LIMIT $1""",max(1,min(int(limit),64)))
    count = 0
    for row in rows:
        if row["child"] == row["parent"]:
            continue
        async with db.connection() as con:
            async with con.transaction():
                inserted = await con.fetchrow(
                    """INSERT INTO aios.knowledge_topic_relation
                       (source_topic_id,target_topic_id,relation_kind,status,
                        source_kind,source_key,source_revision)
                       VALUES ($1,$2,'broader','verified','domain_hierarchy',$3,$3)
                       ON CONFLICT DO NOTHING RETURNING source_topic_id""",
                    row["child"],row["parent"],row["source_key"])
                if inserted:
                    await con.execute(
                        """UPDATE aios.knowledge_topic
                           SET graph_revision=graph_revision+1,updated_at=now()
                           WHERE topic_id=ANY($1::uuid[])""",
                        [row["child"],row["parent"]])
                    count += 1
    return count


async def reconcile_catalog_domain_links_once(db, *, limit: int = 16) -> int:
    """Connect catalog subjects to an *explicitly registered* document domain.

    This is advisory navigation from topic to its source collection, not an
    ontological is-a relation and never a character corpus-access grant.
    The source kind/key match topic_source so source revisions retract this link.
    """
    rows = await db.fetch(
        """SELECT s.topic_id AS child, parent_topic.topic_id AS parent,
                  s.link_kind source_kind,s.source_key,s.source_revision
           FROM aios.knowledge_topic_source s
           JOIN aios.knowledge_topic t ON t.topic_id=s.topic_id
           JOIN aios.corpus_document_domain cdd ON cdd.document_id=s.document_id
             AND t.namespace='catalog:'||cdd.knowledge_domain
           JOIN aios.knowledge_domain d ON d.domain_key=cdd.knowledge_domain
             AND d.enabled
           JOIN aios.knowledge_topic_mention dm ON dm.source_kind='knowledge_domain'
             AND dm.source_key=d.domain_id::text
           JOIN aios.knowledge_topic parent_topic ON parent_topic.topic_id=dm.topic_id
           WHERE t.status<>'retired' AND NOT EXISTS (
              SELECT 1 FROM aios.knowledge_topic_relation r
              WHERE r.source_topic_id=s.topic_id AND r.target_topic_id=parent_topic.topic_id
                AND r.relation_kind='associated'
                AND r.source_kind=s.link_kind AND r.source_key=s.source_key)
           ORDER BY s.created_at,s.topic_id LIMIT $1""",
        max(1,min(int(limit),64)))
    count = 0
    for row in rows:
        if row["child"] == row["parent"]:
            continue
        async with db.connection() as con:
            async with con.transaction():
                inserted = await con.fetchrow(
                    """INSERT INTO aios.knowledge_topic_relation
                       (source_topic_id,target_topic_id,relation_kind,status,
                        source_kind,source_key,source_revision)
                       VALUES ($1,$2,'associated','candidate',$3,$4,$5)
                       ON CONFLICT DO NOTHING RETURNING source_topic_id""",
                    row["child"],row["parent"],row["source_kind"],
                    row["source_key"],row["source_revision"])
                if inserted:
                    await con.execute(
                        """UPDATE aios.knowledge_topic
                           SET graph_revision=graph_revision+1,updated_at=now()
                           WHERE topic_id=ANY($1::uuid[])""",
                        [row["child"],row["parent"]])
                    count += 1
    return count
