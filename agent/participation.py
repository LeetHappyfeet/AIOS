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
        goals = await con.fetch("""SELECT goal_id,goal_text,updated_at FROM aios.character_agent_goal
            WHERE instance_id=$1 AND status='active' ORDER BY priority,created_at,goal_id LIMIT 65""", row["instance_id"])
        facets = await con.fetch("""SELECT facet_id,value,updated_at FROM aios.character_identity_facet
            WHERE character_id=$1 AND status='active' AND facet_type IN ('value','values','interest','interests','preference','preferences','personality','role','constraint')
            ORDER BY facet_type,facet_key LIMIT 65""", row["character_id"])
        relationships = await con.fetch("""SELECT r.relationship_id,e.display_name,e.entity_key,r.updated_at
            FROM aios.character_relationship r JOIN aios.world_entity e ON e.entity_id=r.target_entity_id
            WHERE r.observer_instance_id=$1 ORDER BY r.target_entity_id LIMIT 65""", row["instance_id"])
        return {"names": [row["character_id"], row["display_name"], row["canonical_name"]],
                "goals": [dict(r) for r in goals[:64]], "facets": [{**dict(r), "value": json_value(r["value"])} for r in facets[:64]],
                "relationships": [dict(r) for r in relationships[:64]],
                "truncated": {"goals": len(goals)>64,"facets": len(facets)>64,"relationships": len(relationships)>64}}

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
                            p.subject_norm,p.object_norm,c.claim_kind,c.target_character_id,c.speaker_id,
                            c.resolved_at,ck.epistemic_status,ck.claim_id AS acquired_claim
                            FROM aios.character_participation_pending q
                            JOIN aios.character_participation_experiment x USING(experiment_id)
                            JOIN aios.character_instance ci ON ci.instance_id=x.instance_id
                            LEFT JOIN aios.character_identity i ON i.character_id=ci.character_id
                            LEFT JOIN aios.character_knowledge ck ON ck.instance_id=x.instance_id AND ck.claim_id=q.claim_id
                            LEFT JOIN aios.observation o ON o.claim_id=q.claim_id
                            LEFT JOIN aios.proposition p ON p.proposition_id=o.proposition_id
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
                        # Indexed pair directions; conflict hints remain candidates.
                        conflicts = await con.fetch("""WITH candidates AS (
                            SELECT conflict_id,proposition_b_id AS other FROM aios.proposition_conflict WHERE proposition_a_id=$1
                            UNION ALL SELECT conflict_id,proposition_a_id FROM aios.proposition_conflict WHERE proposition_b_id=$1)
                            SELECT DISTINCT c.conflict_id FROM candidates c
                            JOIN aios.observation o ON o.proposition_id=c.other
                            JOIN aios.character_knowledge ck ON ck.claim_id=o.claim_id AND ck.instance_id=$2
                            WHERE o.observed_at<=$3 LIMIT 8""", item["proposition_id"],item["instance_id"],item["observed_at"])
                        result = propose(dict(item),context,recurrence=int(recurrence),conflicts=[r['conflict_id'] for r in conflicts])
                        snapshot = json.dumps(context,default=str,sort_keys=True)
                        await con.execute("""INSERT INTO aios.character_participation_evaluation
                            (experiment_id,claim_id,instance_id,proposition_id,policy_version,population,
                             reasons,signals,claim_snapshot,context_snapshot,context_version,elapsed_ms)
                            VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,$8::jsonb,$9::jsonb,$10::jsonb,$11,$12)
                            ON CONFLICT DO NOTHING""", *args,item["instance_id"],item["proposition_id"],POLICY_VERSION,
                            result['population'],json.dumps(result['reasons']),json.dumps(result['signals']),
                            json.dumps({key: item[key] for key in (
                                "canonical_text","subject_norm","object_norm","observed_at",
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

    async def inspect(self, instance_id, experiment_id, *, limit=50):
        exp = await self.db.fetchrow("SELECT * FROM aios.character_participation_experiment WHERE instance_id=$1 AND experiment_id=$2",instance_id,experiment_id)
        if not exp:
            raise LookupError("Experiment not found for instance")
        counts = await self.db.fetch("SELECT status,count(*) AS count FROM aios.character_participation_pending WHERE experiment_id=$1 GROUP BY status",experiment_id)
        populations = await self.db.fetch("SELECT population,count(*) AS count,avg(elapsed_ms) AS mean_elapsed_ms FROM aios.character_participation_evaluation WHERE experiment_id=$1 GROUP BY population",experiment_id)
        rows = await self.db.fetch("""SELECT * FROM aios.character_participation_evaluation
            WHERE experiment_id=$1 ORDER BY evaluated_at DESC,claim_id LIMIT $2""",experiment_id,max(1,min(limit,200)))
        total = sum(int(r['count']) for r in populations)
        foreground = sum(int(r['count']) for r in populations if r['population']=='foreground')
        return {'shadow':True,'experiment':dict(exp),'queue':[dict(r) for r in counts],
                'populations':[dict(r) for r in populations],
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
