"""Opt-in shadow participation; no semantic admission or vector writes."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

POLICY_VERSION = "participation-shadow-v1"
COMPARISON_VERSION = "participation-shadow-v2"
RELEVANCE_FACETS = {"value", "values", "interest", "interests", "preference",
                    "preferences", "personality", "role", "constraint"}
logger = logging.getLogger("aios.participation")
STOPWORDS = set("the and that this with from have has was were are for not into about she her his him they them you your its says said like want wants".split())


def tokens(value: Any) -> set[str]:
    return {w for w in re.findall(r"[\w]+", str(value).casefold())
            if len(w) > 2 and w not in STOPWORDS}


def json_value(value):
    """asyncpg returns JSONB strings unless a codec is installed."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            pass
    return value


def value_text(value):
    """Match authored values, never JSON property names."""
    value = json_value(value)
    if isinstance(value, dict):
        return " ".join(value_text(v) for v in value.values())
    if isinstance(value, list):
        return " ".join(value_text(v) for v in value)
    return str(value) if value is not None else ""


def propose(claim: dict, context: dict, *, recurrence: int = 1,
            conflicts: list | None = None) -> dict:
    """Cheap lexical attention hints. Matches never assert semantic truth."""
    text = tokens(claim.get("canonical_text", ""))
    goal_ids = [str(g["goal_id"]) for g in context["goals"]
                if len(text & tokens(g["goal_text"])) >= 2]
    facet_ids = [str(f["facet_id"]) for f in context["facets"]
                 if text & tokens(value_text(f["value"]))]
    relation_ids = [str(r["relationship_id"]) for r in context["relationships"]
                    if any(n and str(n).casefold() in {
                        str(claim.get("subject_norm", "")).casefold(),
                        str(claim.get("object_norm", "")).casefold()}
                           for n in (r.get("display_name"), r.get("entity_key")))]
    names = {str(n).casefold() for n in context["names"] if n}
    involved = (str(claim.get("target_character_id", "")).casefold() in names
                or str(claim.get("speaker_id", "")).casefold() in names
                or str(claim.get("subject_norm", "")).casefold() in names
                or str(claim.get("object_norm", "")).casefold() in names)
    reasons = []
    for name, supported in (("goal_token_overlap", goal_ids),
                            ("identity_token_overlap", facet_ids),
                            ("relationship_entity_match", relation_ids),
                            ("direct_involvement", involved),
                            ("recurrence", recurrence >= 2),
                            ("known_conflict_candidate", conflicts)):
        if supported:
            reasons.append(name)
    unknown = [] if context["facets"] else ["identity_relevance"]
    unknown.extend(name + "_truncated" for name, truncated in context.get("truncated", {}).items() if truncated)
    if involved or goal_ids or facet_ids or conflicts or recurrence >= 3:
        population = "foreground"
    elif reasons or unknown:
        population = "latent"
    else:
        population = "background"
    return {"population": population, "reasons": reasons or ["no_supported_relevance"],
            "signals": {"goal_ids": goal_ids, "facet_ids": facet_ids,
                        "relationship_ids": relation_ids, "direct_involvement": involved,
                        "recurrence_capped_at_four": recurrence,
                        "conflict_ids": [str(c) for c in (conflicts or [])],
                        "unknown": unknown}}


def propose_v2(claim: dict, context: dict, *, recurrence: int | None = None,
               conflicts: list | None = None) -> dict:
    """Conservative shadow policy. Access to evidence is not significance."""
    baseline = propose(claim, context, recurrence=1, conflicts=conflicts)
    signals = baseline["signals"]
    names = {str(n).strip().casefold() for n in context["names"] if n}
    matches = lambda key: str(claim.get(key) or "").strip().casefold() in names
    involvement = {
        "authorship": matches("speaker_id"),
        # Source routing target is not proof that this sentence addresses them.
        "source_target": matches("target_character_id"),
        "claim_participation": matches("subject_norm") or matches("object_norm"),
        "addressed": None,
        "scene_presence": None,
    }
    subject = str(claim.get("subject_norm") or "").strip().casefold()
    predicate = str(claim.get("predicate_norm") or "").strip().casefold()
    obj = str(claim.get("object_norm") or "").strip().casefold()
    quality_reasons = []
    if not subject or subject in {"_", "which", "something", "anything", "nothing", "it"}:
        quality_reasons.append("unresolved_or_generic_subject")
    if not predicate or predicate == "_":
        quality_reasons.append("missing_predicate")
    # Missing objects can be valid for explicit intransitive predicates.
    if obj in {"", "_"} and predicate not in {"hesitate", "arrive", "leave", "sleep", "wait", "smile", "laugh", "nod", "pause"}:
        quality_reasons.append("insufficient_predicate_arguments")
    usable = not quality_reasons
    unknown = list(signals["unknown"])
    for name in ("goals", "relationships"):
        if not context[name]:
            unknown.append(name + "_relevance")
    missing = [key for key in ("speaker_id", "target_character_id", "predicate_norm", "source_sentence", "dag_node_id") if key not in claim]
    if missing:
        unknown.append("legacy_snapshot_missing_evidence")
    if recurrence is None:
        unknown.append("independent_recurrence")
    relevance = bool(signals["goal_ids"] or signals["facet_ids"] or conflicts)
    relationship_repeat = bool(signals["relationship_ids"] and (recurrence or 0) >= 2)
    participant_repeat = involvement["claim_participation"] and (recurrence or 0) >= 3
    foreground = usable and (relevance or relationship_repeat or participant_repeat)
    reasons = [r for r in baseline["reasons"] if r not in {"direct_involvement", "recurrence", "no_supported_relevance"}]
    if any(v is True for v in involvement.values()):
        reasons.append("participation_evidence")
    if (recurrence or 0) >= 2:
        reasons.append("independent_recurrence")
    if not usable:
        reasons.append("insufficient_semantic_content")
    return {"policy_version": COMPARISON_VERSION,
            "population": "foreground" if foreground else "latent" if reasons or unknown else "background",
            "reasons": reasons or ["no_supported_relevance"],
            "signals": {**signals, "direct_involvement": involvement["claim_participation"],
                        "involvement": involvement, "representation_quality": {
                            "usable": usable, "reasons": quality_reasons},
                        "independent_occurrences_capped_at_four": recurrence,
                        "unknown": sorted(set(unknown)), "missing_snapshot_fields": missing}}


class ParticipationService:
    def __init__(self, db):
        self.db = db

    async def start(self, instance_id, *, since_at=None, duration_minutes=60, max_claims=100):
        if not 1 <= max_claims <= 1000 or not 1 <= duration_minutes <= 1440:
            raise ValueError("max_claims must be 1..1000 and duration_minutes 1..1440")
        now = datetime.now(timezone.utc)
        since_at = since_at or now
        if since_at.tzinfo is None or since_at > now or since_at < now - timedelta(days=7):
            raise ValueError("since_at must be timezone-aware and within the past seven days")
        async with self.db.connection() as con:
            async with con.transaction():
                await con.execute("SET LOCAL statement_timeout='2000ms'")
                if not await con.fetchval("SELECT 1 FROM aios.character_instance WHERE instance_id=$1", instance_id):
                    raise LookupError("Unknown instance")
                exp = await con.fetchrow("""INSERT INTO aios.character_participation_experiment
                    (instance_id,policy_version,since_at,until_at,max_claims)
                    VALUES($1,$2,$3,$4,$5) RETURNING *""", instance_id, POLICY_VERSION,
                    since_at, now + timedelta(minutes=duration_minutes), max_claims)
                rows = await con.fetch("""INSERT INTO aios.character_participation_pending(experiment_id,claim_id)
                    SELECT $1,ck.claim_id FROM aios.character_knowledge ck
                    WHERE ck.instance_id=$2 AND ck.updated_at BETWEEN $3 AND $4
                    ORDER BY ck.updated_at DESC,ck.claim_id LIMIT $5
                    ON CONFLICT DO NOTHING RETURNING claim_id""", exp["experiment_id"],
                    instance_id, since_at, now, max_claims)
                await con.execute("UPDATE aios.character_participation_experiment SET enqueued_count=$2 WHERE experiment_id=$1",
                                  exp["experiment_id"], len(rows))
                return {**dict(exp), "enqueued_count": len(rows), "shadow": True}

    async def stop(self, instance_id, experiment_id):
        async with self.db.connection() as con:
            async with con.transaction():
                await con.execute("SET LOCAL statement_timeout='2000ms'")
                row = await con.fetchrow("""UPDATE aios.character_participation_experiment
                    SET status='stopped' WHERE instance_id=$1 AND experiment_id=$2 RETURNING experiment_id""",
                    instance_id, experiment_id)
                if not row:
                    raise LookupError("Experiment not found for instance")
                await con.execute("""UPDATE aios.character_participation_pending
                    SET status='skipped',last_error='experiment stopped'
                    WHERE experiment_id=$1 AND status='pending'""", experiment_id)

    async def _context(self, con, row):
        # Read bounded excluded rows too, so empty relevance arrays are explainable.
        goal_rows = await con.fetch("""SELECT goal_id,goal_text,status,meta,updated_at
            FROM aios.character_agent_goal WHERE instance_id=$1
            ORDER BY (status='active') DESC,priority,created_at,goal_id LIMIT 65""", row["instance_id"])
        facet_rows = await con.fetch("""SELECT facet_id,facet_type,facet_key,value,status,
            authority,source_id,source_field,updated_at FROM aios.character_identity_facet
            WHERE character_id=$1 AND status='active'
            ORDER BY (facet_type=ANY($2::text[])) DESC,facet_type,facet_key LIMIT 65""",
            row["character_id"], sorted(RELEVANCE_FACETS))
        candidates = await con.fetch("""SELECT candidate_id,facet_type,disposition
            FROM aios.character_identity_candidate WHERE character_id=$1
            ORDER BY candidate_id LIMIT 65""", row["character_id"])
        relationship_rows = await con.fetch("""SELECT r.relationship_id,r.relationship_type,
            r.affinity,r.trust,r.familiarity,e.display_name,e.entity_key,r.updated_at
            FROM aios.character_relationship r LEFT JOIN aios.world_entity e ON e.entity_id=r.target_entity_id
            WHERE r.observer_instance_id=$1 ORDER BY r.target_entity_id LIMIT 65""", row["instance_id"])
        goals = [dict(r) for r in goal_rows[:64] if r["status"] == "active"]
        facets = [{**dict(r), "value": json_value(r["value"])} for r in facet_rows[:64]
                  if r["facet_type"] in RELEVANCE_FACETS]
        relationships = [dict(r) for r in relationship_rows[:64] if r["entity_key"] or r["display_name"]]
        return {"names": list(dict.fromkeys(n for n in (
                    row["character_id"], row["display_name"], row["canonical_name"]) if n)),
                "goals": goals, "facets": facets, "relationships": relationships,
                "coverage": {
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                    "temporal_basis": "evaluation_time_not_historical",
                    "goals": {"scope": "instance", "instance_id": str(row["instance_id"]),
                              "eligible": len(goals), "sampled_rows": [dict(r) for r in goal_rows[:64]]},
                    "facets": {"scope": "character", "character_id": row["character_id"],
                               "eligible": len(facets), "excluded_types": sorted({r["facet_type"] for r in facet_rows[:64] if r["facet_type"] not in RELEVANCE_FACETS}),
                               "candidate_sample": [dict(r) for r in candidates[:64]],
                               "candidates_truncated": len(candidates)>64},
                    "relationships": {"scope": "instance", "eligible": len(relationships),
                                      "missing_entities": len(relationship_rows[:64])-len(relationships)}},
                "truncated": {"goals": len(goal_rows)>64,"facets": len(facet_rows)>64,"relationships": len(relationship_rows)>64}}

    async def process_pending(self, *, limit=16, budget_seconds=5.0):
        deadline = time.monotonic() + max(0.1, min(budget_seconds, 30))
        processed = 0
        for _ in range(max(1, min(limit, 100))):
            if time.monotonic() >= deadline:
                break
            item = None
            started = time.monotonic()
            try:
                async with self.db.connection() as con:
                    async with con.transaction():
                        await con.execute("SET LOCAL statement_timeout='1500ms'")
                        item = await con.fetchrow("""SELECT q.*,x.instance_id,x.until_at,ci.character_id,
                            i.display_name,i.canonical_name,o.proposition_id,o.observed_at,p.canonical_text,
                            p.subject_norm,p.predicate_norm,p.object_norm,o.dag_node_id,o.source_key,
                            cc.raw_text AS source_sentence,c.claim_kind,c.target_character_id,c.speaker_id,
                            c.resolved_at,ck.epistemic_status,ck.claim_id AS acquired_claim
                            FROM aios.character_participation_pending q
                            JOIN aios.character_participation_experiment x USING(experiment_id)
                            JOIN aios.character_instance ci ON ci.instance_id=x.instance_id
                            LEFT JOIN aios.character_identity i ON i.character_id=ci.character_id
                            LEFT JOIN aios.character_knowledge ck ON ck.instance_id=x.instance_id AND ck.claim_id=q.claim_id
                            LEFT JOIN aios.observation o ON o.claim_id=q.claim_id
                            LEFT JOIN aios.proposition p ON p.proposition_id=o.proposition_id
                            LEFT JOIN aios.claim_candidate cc ON cc.claim_id=q.claim_id
                            LEFT JOIN aios.claim_context_resolution c ON c.claim_id=q.claim_id
                            WHERE q.status='pending' AND q.ready_at<=now() AND x.status='running'
                              AND x.policy_version=$1
                            ORDER BY q.ready_at,q.experiment_id,q.claim_id
                            LIMIT 1 FOR UPDATE OF q SKIP LOCKED""", POLICY_VERSION)
                        if not item:
                            break
                        args = (item["experiment_id"], item["claim_id"])
                        if not item["acquired_claim"]:
                            await con.execute("UPDATE aios.character_participation_pending SET status='skipped',last_error='visibility withdrawn' WHERE experiment_id=$1 AND claim_id=$2", *args)
                            continue
                        if not item["proposition_id"] or item["claim_kind"] in (None, 'UNKNOWN'):
                            await con.execute("""UPDATE aios.character_participation_pending
                                SET ready_at=now()+interval '30 seconds',
                                    status=CASE WHEN now()>$3::timestamptz+interval '10 minutes' THEN 'skipped' ELSE 'pending' END,
                                    last_error='awaiting resolved context' WHERE experiment_id=$1 AND claim_id=$2""",
                                *args, item["queued_at"])
                            continue
                        context = await self._context(con, item)
                        recurrence = await con.fetchval("""SELECT count(*) FROM (
                            SELECT DISTINCT o.claim_id FROM aios.observation o
                            JOIN aios.character_knowledge ck ON ck.claim_id=o.claim_id AND ck.instance_id=$1
                            WHERE o.proposition_id=$2 AND o.observed_at<=$3 LIMIT 4) r""",
                            item["instance_id"],item["proposition_id"],item["observed_at"])
                        # One source node/document counts once, regardless of extracted claim count.
                        independent = await con.fetchval("""SELECT count(*) FROM (
                            SELECT DISTINCT COALESCE(o.dag_node_id::text,o.document_id::text) AS occurrence
                            FROM aios.observation o
                            JOIN aios.character_knowledge ck ON ck.claim_id=o.claim_id AND ck.instance_id=$1
                            WHERE o.proposition_id=$2 AND o.observed_at<=$3
                              AND (o.dag_node_id IS NOT NULL OR o.document_id IS NOT NULL)
                            LIMIT 4) r""", item["instance_id"],item["proposition_id"],item["observed_at"])
                        # Indexed pair directions; conflict hints remain candidates.
                        conflicts = await con.fetch("""WITH candidates AS (
                            SELECT conflict_id,proposition_b_id AS other FROM aios.proposition_conflict WHERE proposition_a_id=$1
                            UNION ALL SELECT conflict_id,proposition_a_id FROM aios.proposition_conflict WHERE proposition_b_id=$1)
                            SELECT DISTINCT c.conflict_id FROM candidates c
                            JOIN aios.observation o ON o.proposition_id=c.other
                            JOIN aios.character_knowledge ck ON ck.claim_id=o.claim_id AND ck.instance_id=$2
                            WHERE o.observed_at<=$3 LIMIT 8""", item["proposition_id"],item["instance_id"],item["observed_at"])
                        result = propose(dict(item),context,recurrence=int(recurrence),conflicts=[r['conflict_id'] for r in conflicts])
                        comparison = propose_v2(dict(item), context, recurrence=int(independent),
                                                conflicts=[r['conflict_id'] for r in conflicts])
                        comparison["evaluation_mode"] = "paired_live"
                        result["signals"]["comparison"] = comparison
                        snapshot = json.dumps(context,default=str,sort_keys=True)
                        await con.execute("""INSERT INTO aios.character_participation_evaluation
                            (experiment_id,claim_id,instance_id,proposition_id,policy_version,population,
                             reasons,signals,claim_snapshot,context_snapshot,context_version,elapsed_ms)
                            VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,$8::jsonb,$9::jsonb,$10::jsonb,$11,$12)
                            ON CONFLICT DO NOTHING""", *args,item["instance_id"],item["proposition_id"],POLICY_VERSION,
                            result['population'],json.dumps(result['reasons']),json.dumps(result['signals']),
                            json.dumps({key: item.get(key) for key in (
                                "canonical_text","subject_norm","predicate_norm","object_norm","observed_at",
                                "speaker_id","target_character_id","source_sentence","dag_node_id","source_key",
                                "claim_kind","resolved_at","epistemic_status")},default=str),snapshot,
                            hashlib.sha256(snapshot.encode()).hexdigest(),(time.monotonic()-started)*1000)
                        await con.execute("UPDATE aios.character_participation_pending SET status='evaluated',last_error=NULL WHERE experiment_id=$1 AND claim_id=$2", *args)
                        processed += 1
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                logger.warning("Shadow participation evaluation failed (SQLSTATE %s): %s",
                               getattr(exc, "sqlstate", None), error)
                if item:
                    async with self.db.connection() as con:
                        async with con.transaction():
                            await con.execute("SET LOCAL statement_timeout='1500ms'")
                            await con.execute("""UPDATE aios.character_participation_pending SET error_count=error_count+1,
                                status=CASE WHEN error_count>=2 THEN 'failed' ELSE 'pending' END,
                                ready_at=now()+interval '30 seconds',last_error=$3
                                WHERE experiment_id=$1 AND claim_id=$2 AND status='pending'""",
                                item['experiment_id'],item['claim_id'],error[:2000])
                break
        return processed

    async def audit_context(self, instance_id):
        async with self.db.connection() as con:
            async with con.transaction():
                await con.execute("SET LOCAL statement_timeout='2000ms'")
                row = await con.fetchrow("""SELECT ci.instance_id,ci.character_id,i.display_name,i.canonical_name
                    FROM aios.character_instance ci LEFT JOIN aios.character_identity i USING(character_id)
                    WHERE ci.instance_id=$1""", instance_id)
                if not row:
                    raise LookupError("Unknown instance")
                return {"shadow": True, "context": await self._context(con, row)}

    async def compare_existing(self, instance_id, experiment_id, *, limit=50):
        """Replay frozen snapshots; missing legacy evidence stays explicitly unknown."""
        async with self.db.connection() as con:
            async with con.transaction():
                await con.execute("SET LOCAL statement_timeout='2000ms'")
                if not await con.fetchval("SELECT 1 FROM aios.character_participation_experiment WHERE instance_id=$1 AND experiment_id=$2", instance_id, experiment_id):
                    raise LookupError("Experiment not found for instance")
                rows = await con.fetch("""SELECT * FROM aios.character_participation_evaluation
                    WHERE instance_id=$1 AND experiment_id=$2 AND NOT (signals ? 'comparison')
                    ORDER BY evaluated_at,claim_id LIMIT $3 FOR UPDATE SKIP LOCKED""",
                    instance_id,experiment_id,max(1,min(limit,100)))
                for row in rows:
                    signals = json_value(row["signals"])
                    result = propose_v2(json_value(row["claim_snapshot"]),json_value(row["context_snapshot"]),
                        recurrence=None, conflicts=signals.get("conflict_ids"))
                    result["evaluation_mode"] = "frozen_legacy_replay"
                    await con.execute("""UPDATE aios.character_participation_evaluation
                        SET signals=jsonb_set(signals,'{comparison}',$3::jsonb)
                        WHERE experiment_id=$1 AND claim_id=$2""", experiment_id,row["claim_id"],json.dumps(result))
                return {"shadow": True, "compared": len(rows), "policy_version": COMPARISON_VERSION,
                        "note": "Legacy snapshots lack source involvement and independent recurrence; neither is inferred."}

    async def inspect(self, instance_id, experiment_id, *, limit=50):
        exp = await self.db.fetchrow("SELECT * FROM aios.character_participation_experiment WHERE instance_id=$1 AND experiment_id=$2",instance_id,experiment_id)
        if not exp:
            raise LookupError("Experiment not found for instance")
        counts = await self.db.fetch("SELECT status,count(*) AS count FROM aios.character_participation_pending WHERE experiment_id=$1 GROUP BY status",experiment_id)
        populations = await self.db.fetch("SELECT population,count(*) AS count,avg(elapsed_ms) AS mean_elapsed_ms FROM aios.character_participation_evaluation WHERE experiment_id=$1 GROUP BY population",experiment_id)
        rows = await self.db.fetch("""SELECT * FROM aios.character_participation_evaluation
            WHERE experiment_id=$1 ORDER BY evaluated_at DESC,claim_id LIMIT $2""",experiment_id,max(1,min(limit,200)))
        comparisons = await self.db.fetch("""SELECT population AS baseline_population,
            signals->'comparison'->>'population' AS comparison_population,
            signals->'comparison'->>'evaluation_mode' AS evaluation_mode,count(*) AS count
            FROM aios.character_participation_evaluation
            WHERE experiment_id=$1 AND signals ? 'comparison'
            GROUP BY 1,2,3""",experiment_id)
        total = sum(int(r['count']) for r in populations)
        foreground = sum(int(r['count']) for r in populations if r['population']=='foreground')
        return {'shadow':True,'experiment':dict(exp),'queue':[dict(r) for r in counts],
                'populations':[dict(r) for r in populations],
                'comparison_version':COMPARISON_VERSION,'comparisons':[dict(r) for r in comparisons],
                'proposed_foreground_share':foreground/total if total else None,
                'evaluations':[{**dict(r), **{k:json_value(r[k]) for k in (
                    'reasons','signals','claim_snapshot','context_snapshot')}} for r in rows],
                'metric_note':'Proposed participation only; downstream usefulness requires follow-up review.'}


async def run_worker(*, once=False):
    from aios_app.config import settings
    from aios_app.db import Database
    db = Database(settings.db_dsn,min_size=1,max_size=2)
    await db.connect()
    try:
        while True:
            try:
                count = await ParticipationService(db).process_pending()
            except Exception:
                logger.exception("Shadow participation worker batch failed")
                if once:
                    raise
                await asyncio.sleep(2.0)
                continue
            if once:
                return
            await asyncio.sleep(0.25 if count else 2.0)
    finally:
        await db.close()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Independent shadow participation worker')
    parser.add_argument('--once',action='store_true',help='Evaluate at most one bounded batch and exit')
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker(once=parser.parse_args().once))
