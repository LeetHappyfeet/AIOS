"""Causally bounded reconciliation of goals recovered from historical DAG turns.

This processes cognition receipts written by gap-aware catch-up, not arbitrary
old messages or another character's timeline. A later GOAL unit for the same
topic supersedes older candidate evidence, including negative withdrawals.
Existing later/terminal managed goals are never revived by archaeology.
"""
from __future__ import annotations

import json
import logging
from uuid import UUID

from aios_app.epistemic.goals import CharacterGoalService, valid_goal_objective

logger = logging.getLogger("aios.epistemic.deferred_goals")
LIMIT = 64


def _meta(value) -> dict:
    return CharacterGoalService._json_object(value)


async def reconcile_deferred_goals(db, *, instance_id: UUID, limit: int = LIMIT) -> dict:
    if not 1 <= limit <= LIMIT:
        raise ValueError("Deferred goal reconciliation limit must be 1..64")
    counts = {"considered": 0, "applied": 0, "superseded": 0,
              "protected": 0, "rejected": 0, "remaining": 0}
    async with db.connection() as con:
        async with con.transaction():
            await con.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                f"goal-cognition::{instance_id}",
            )
            # Pin the exact current source head for the entire reconciliation.
            current = await con.fetchrow(
                """SELECT rs.source_timeline_id,rs.source_head_node_id
                   FROM aios.character_runtime_state rs
                   WHERE rs.instance_id=$1 FOR UPDATE""", instance_id,
            )
            if not current or not current["source_head_node_id"]:
                return counts
            lineage = await con.fetch(
                """WITH RECURSIVE chain AS (
                     SELECT dn.node_id,dn.timeline_id,ARRAY[dn.node_id]::uuid[] AS visited,
                            0 AS depth
                     FROM aios.dag_node dn WHERE dn.node_id=$1 AND dn.timeline_id=$2
                     UNION ALL
                     SELECT p.node_id,p.timeline_id,ch.visited||p.node_id,ch.depth+1
                     FROM chain ch
                     JOIN aios.dag_edge de
                       ON de.timeline_id=ch.timeline_id AND de.child_node_id=ch.node_id
                     JOIN aios.dag_node p
                       ON p.node_id=de.parent_node_id AND p.timeline_id=de.timeline_id
                     WHERE ch.depth<4096 AND NOT p.node_id=ANY(ch.visited)
                   ) SELECT DISTINCT node_id FROM chain""",
                current["source_head_node_id"], current["source_timeline_id"],
            )
            node_ids = [r["node_id"] for r in lineage]
            if not node_ids:
                return counts
            # Read both deferred and already admitted later units to ensure an
            # old positive objective cannot override a more recent withdrawal.
            units = await con.fetch(
                """SELECT u.unit_id,u.commit_id,u.ordinal,u.text,u.topic_key,
                          u.polarity,u.confidence,u.salience,u.meta,u.status,
                          c.node_id,c.event_id,c.summary
                   FROM aios.message_cognitive_unit u
                   JOIN aios.message_cognitive_commit c ON c.commit_id=u.commit_id
                   WHERE c.instance_id=$1 AND c.node_id=ANY($2::uuid[])
                     AND u.claim_kind='GOAL'
                     AND u.status='active'
                     AND COALESCE(u.meta->>'character_owned','false')='true'
                   ORDER BY c.event_id,u.ordinal,u.unit_id""",
                instance_id, node_ids,
            )
            # All observations, including later live commitments, participate
            # in latest-source selection. Do not manufacture intermediate goal
            # completion/creation effects during a retrospective replay.
            latest: dict[str, tuple[int,int,str]] = {}
            for unit in units:
                latest[str(unit["topic_key"])] = (
                    int(unit["event_id"]), int(unit["ordinal"]),
                    str(unit["unit_id"]),
                )
            pending = [u for u in units
                       if _meta(u["summary"]).get("historical_catchup")
                       and _meta(u["summary"]).get("goal_projection_deferred")
                       and "historical_goal_reconciliation" not in _meta(u["meta"])]
            goals = CharacterGoalService(con)
            for unit in pending[:limit]:
                counts["considered"] += 1
                meta = _meta(unit["meta"])
                topic = str(unit["topic_key"] or "")
                status = "rejected"
                if meta.get("horizon") == "immediate":
                    counts["rejected"] += 1
                    status = "immediate_action_not_managed_goal"
                elif not topic or not valid_goal_objective(meta.get("objective") or unit["text"]):
                    counts["rejected"] += 1
                    status = "invalid_objective"
                elif latest.get(topic, (None,None,None))[2] != str(unit["unit_id"]):
                    counts["superseded"] += 1
                    status = "superseded_by_later_source_evidence"
                else:
                    # A managed goal may have advanced independently of this
                    # catch-up. Neither a later authoritative source nor a
                    # foreign goal owner may be overwritten by historical text.
                    existing = await con.fetch(
                        """SELECT g.goal_id,g.status,g.meta,g.source_node_id,
                                  dn.event_id AS source_event_id
                           FROM aios.character_agent_goal g
                           LEFT JOIN aios.dag_node dn ON dn.node_id=g.source_node_id
                           WHERE g.instance_id=$1
                             AND g.meta->>'semantic_topic_key'=$2
                           ORDER BY g.created_at DESC,g.goal_id DESC
                           FOR UPDATE OF g""", instance_id, topic,
                    )
                    later_goal = any(
                        r["source_event_id"] is None or
                        int(r["source_event_id"]) > int(unit["event_id"]) or
                        _meta(r["meta"]).get("source") not in (None, "message_cognition")
                        for r in existing
                    )
                    if later_goal:
                        counts["protected"] += 1
                        status = "newer_or_external_managed_goal_preserved"
                    else:
                        result = await goals.reconcile_evidence(
                            instance_id=instance_id,
                            text=str(unit["text"]), topic_key=topic,
                            polarity=int(unit["polarity"]),
                            source_node_id=unit["node_id"],
                            source_unit_id=unit["unit_id"],
                            confidence=float(unit["confidence"] or 0),
                            salience=float(unit["salience"] or 0),
                            intent_type=meta.get("intent_type"),
                            horizon=meta.get("horizon"),
                            objective=meta.get("objective"),
                            refresh_scene=False,
                        )
                        if result is not None:
                            counts["applied"] += 1
                            status = "applied"
                        else:
                            counts["protected"] += 1
                            status = ("negative_without_active_goal" if
                                      int(unit["polarity"]) < 0 else
                                      "terminal_or_existing_goal_preserved")
                await con.execute(
                    """UPDATE aios.message_cognitive_unit
                       SET meta=meta || jsonb_build_object(
                         'historical_goal_reconciliation',$2::text,
                         'historical_goal_reviewed_at',now()::text)
                       WHERE unit_id=$1""",
                    unit["unit_id"], status,
                )
                await con.execute(
                    """UPDATE aios.message_cognitive_commit c
                       SET summary=summary || jsonb_build_object(
                         'goal_projection_deferred',false,
                         'goal_reconciliation_version','historical-goal-v1')
                       WHERE c.commit_id=$1
                         AND NOT EXISTS (
                           SELECT 1 FROM aios.message_cognitive_unit u
                           WHERE u.commit_id=c.commit_id AND u.claim_kind='GOAL'
                             AND COALESCE(u.meta->>'character_owned','false')='true'
                             AND u.status='active'
                             AND NOT (u.meta ? 'historical_goal_reconciliation')
                         )""", unit["commit_id"],
                )
            counts["remaining"] = max(0, len(pending)-counts["considered"])
    if counts["applied"]:
        # Refresh only the actual current-head scene after the commit; never
        # project a source node being revisited historically.
        from aios_app.epistemic.scene_resolver import CharacterSceneProjector
        try:
            await CharacterSceneProjector(db).refresh(instance_id)
        except LookupError:
            logger.info("Deferred goal scene refresh stale; newer source head owns it")
    logger.info("Historical goal reconciliation instance=%s result=%s",instance_id,counts)
    return counts
