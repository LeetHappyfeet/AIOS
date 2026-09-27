from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID

from aios_app.db import Database
from aios_app.epistemic.goals import CharacterGoalService
from aios_app.epistemic.message_cognition import cognition_topic_key
from aios_app.inference import InferenceBroker, InferenceRequest, InferenceUnavailable


_ALLOWED_KINDS={"GOAL","BELIEF","STATE","RELATIONSHIP","RULE","EVENT"}
class MessageCognitionEnricher:
    """Bounded LLM adjudication for semantic units missed by the cheap parser.

    The model has classification authority only. Database admission, ownership,
    lifecycle and goal creation remain deterministic.
    """
    def __init__(self,db: Database):
        self.db=db
        self.broker=InferenceBroker(db)

    async def run(self,*,instance_id: UUID,node_id: UUID) -> int:
        row=await self.db.fetchrow(
            """SELECT c.commit_id,c.summary,c.character_id,c.speaker_id,c.speaker_role,
                      c.viewpoint_id,c.event_id
               FROM aios.message_cognitive_commit c
               WHERE c.instance_id=$1 AND c.node_id=$2""",instance_id,node_id)
        if not row:
            return 0
        summary=CharacterGoalService._json_object(row["summary"])
        sentences=list(summary.get("ambiguous_sentences") or [])[:4]
        if not sentences or not summary.get("enrichment_pending"):
            return 0

        prompt=(
            "Classify only durable or cognitively meaningful information expressed by the "
            "character in these excerpts. Do not invent motives. A GOAL requires an intention, "
            "commitment, plan, chosen objective, or persistent desire belonging to the character; "
            "requests for another person to act are not the character's goal unless the character "
            "is explicitly trying to cause that outcome. Distinguish BELIEF, STATE, RELATIONSHIP, "
            "RULE, EVENT, GOAL, or NONE. For GOAL only, also classify intent_type as "
            "desire|objective|plan|commitment|immediate_intention and horizon as "
            "immediate|scene|session|persistent, and provide objective as a concise "
            "content phrase. Return {\\\"units\\\":[{\\\"kind\\\":...,"
            "\\\"text\\\":...,\\\"polarity\\\":1|-1,\\\"confidence\\\":0..1,"
            "\\\"source_index\\\":0..N,\\\"intent_type\\\":...,\\\"horizon\\\":...,"
            "\\\"objective\\\":...}]}. At most 4 units.\n"
            f"Character: {row['character_id']}\nSpeaker: {row['speaker_id']}\n"
            "Excerpts:\n"+ "\n".join(f"[{i}] {s}" for i,s in enumerate(sentences))
        )
        try:
            result=await self.broker.infer(InferenceRequest(
                instance_id=instance_id,worker_class="message_cognition",
                prompt=prompt,allowed_actions={},output_schema={"type":"object"},
                temperature=0.0,max_tokens=450))
        except InferenceUnavailable:
            # Optional enrichment must never make deterministic cognition fail.
            return 0

        raw=result.response.raw
        units=raw.get("units") if isinstance(raw,dict) else None
        admitted=0
        if isinstance(units,list):
            for item in units[:4]:
                if not isinstance(item,dict):
                    continue
                kind=str(item.get("kind") or "").upper()
                text=" ".join(str(item.get("text") or "").split())[:500]
                try: confidence=float(item.get("confidence",0))
                except (TypeError,ValueError): confidence=0
                try: source_index=int(item.get("source_index",-1))
                except (TypeError,ValueError): source_index=-1
                polarity=-1 if int(item.get("polarity",1) or 1)<0 else 1
                if kind not in _ALLOWED_KINDS or not text or confidence<0.72:
                    continue
                if source_index<0 or source_index>=len(sentences):
                    continue
                intent_type=str(item.get("intent_type") or "").lower() if kind=="GOAL" else ""
                horizon=str(item.get("horizon") or "").lower() if kind=="GOAL" else ""
                objective=" ".join(str(item.get("objective") or "").split())[:300] if kind=="GOAL" else ""
                if kind=="GOAL":
                    if intent_type not in {"desire","objective","plan","commitment","immediate_intention"}:
                        continue
                    if horizon not in {"immediate","scene","session","persistent"} or not objective:
                        continue
                topic=cognition_topic_key(
                    text, character_id=str(row["character_id"]),
                    owner=str(row["character_id"]), kind=kind,
                    objective=objective or None)
                unit=await self.db.execute_returning_row(
                    """INSERT INTO aios.message_cognitive_unit(
                         commit_id,ordinal,claim_kind,text,topic_key,polarity,
                         salience,confidence,status,meta)
                       SELECT $1,COALESCE(max(ordinal),-1)+1,$2,$3,$4,$5,
                              CASE WHEN $2='GOAL' THEN .86 ELSE .72 END,$6,'active',$7::jsonb
                       FROM aios.message_cognitive_unit WHERE commit_id=$1
                       ON CONFLICT (commit_id,ordinal) DO NOTHING
                       RETURNING unit_id""",
                    row["commit_id"],kind,text,topic,polarity,confidence,
                    json.dumps({"character_owned":True,"semantic_owner":str(row["character_id"]),
                                "source":"bounded_inference","source_index":source_index,
                                "source_text":sentences[source_index],
                                "inference_request_id":str(result.request_id),
                                "intent_type":intent_type or None,"horizon":horizon or None,
                                "objective":objective or None}))
                if not unit:
                    continue
                admitted+=1
                if kind=="GOAL" and horizon not in {"immediate"}:
                    await CharacterGoalService(self.db).reconcile_evidence(
                        instance_id=instance_id,text=text,topic_key=topic,polarity=polarity,
                        source_node_id=node_id,source_unit_id=unit["unit_id"],
                        confidence=confidence,salience=.86,intent_type=intent_type,
                        horizon=horizon,objective=objective)

        await self.db.execute(
            """UPDATE aios.message_cognitive_commit
               SET summary=summary || jsonb_build_object(
                   'enrichment_pending',false,'enrichment_request_id',$3,
                   'enrichment_admitted',$4),
                   enrichment_completed_at=now()
               WHERE instance_id=$1 AND node_id=$2""",
            instance_id,node_id,str(result.request_id),admitted)
        return admitted
