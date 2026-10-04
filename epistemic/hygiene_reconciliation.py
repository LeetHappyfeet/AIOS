"""Operator-controlled, source-bound Semantic Hygiene Reconciliation V1.

A shadow finding cannot mutate knowledge. Proposal re-verifies the immutable
shadow source. Apply requires a named operator plus an explicit environment
flag; the database function independently enforces a transaction-local opt-in.
Nothing runs automatically from the semantic hygiene shadow scheduler.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from uuid import UUID

from aios_app.config import settings
from aios_app.db import Database


async def propose(
    db: Database,
    *,
    audit_id: UUID,
    claim_id: UUID,
    frame_id: UUID,
    proposition_id: UUID,
    action: str,
) -> UUID:
    row = await db.fetchrow(
        """SELECT aios.propose_semantic_hygiene_adjudication(
               $1::uuid,$2::uuid,$3::uuid,$4::uuid,$5::text
           ) AS adjudication_id""",
        audit_id, claim_id, frame_id, proposition_id, action,
    )
    return row["adjudication_id"]


async def apply(db: Database, *, adjudication_id: UUID, actor: str) -> dict:
    if os.getenv("AIOS_SEMANTIC_HYGIENE_APPLY_ENABLED", "0").lower() not in {
        "1", "true", "yes", "on",
    }:
        raise RuntimeError(
            "Live semantic hygiene reconciliation is disabled. Explicitly "
            "set AIOS_SEMANTIC_HYGIENE_APPLY_ENABLED=1 only after source review."
        )
    if not actor.strip():
        raise ValueError("A nonempty adjudicating operator is required")
    # Same connection and transaction are essential: SET LOCAL must never
    # authorize a different pooled session or remain enabled after commit.
    async with db.connection() as con:
        async with con.transaction():
            await con.execute(
                "SELECT set_config('aios.semantic_hygiene_apply_enabled','on',true)"
            )
            row = await con.fetchrow(
                """SELECT aios.apply_semantic_hygiene_adjudication(
                       $1::uuid,$2::text
                   ) AS result""",
                adjudication_id, actor.strip(),
            )
    payload = row["result"]
    return json.loads(payload) if isinstance(payload, str) else dict(payload)


async def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="Propose or explicitly apply one source-verified hygiene adjudication"
    )
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--propose-audit", type=UUID)
    actions.add_argument("--apply-id", type=UUID)
    parser.add_argument("--claim-id", type=UUID)
    parser.add_argument("--frame-id", type=UUID)
    parser.add_argument("--proposition-id", type=UUID)
    parser.add_argument(
        "--action", choices=("suppress_independent", "demote_to_context")
    )
    parser.add_argument("--actor", default="")
    args = parser.parse_args()
    db = Database(settings.db_dsn, min_size=1, max_size=2)
    await db.connect()
    try:
        if args.propose_audit:
            if not all((args.claim_id, args.frame_id, args.proposition_id, args.action)):
                parser.error(
                    "--propose-audit also requires --claim-id, --frame-id, "
                    "--proposition-id, and --action"
                )
            result = await propose(
                db, audit_id=args.propose_audit, claim_id=args.claim_id,
                frame_id=args.frame_id, proposition_id=args.proposition_id,
                action=args.action,
            )
            print(json.dumps({
                "adjudication_id": str(result),
                "status": "proposed",
                "live_knowledge_changed": False,
            }, indent=2))
        else:
            if not args.actor.strip():
                parser.error("--apply-id requires a named --actor")
            result = await apply(db, adjudication_id=args.apply_id, actor=args.actor)
            print(json.dumps(result, default=str, indent=2))
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(_cli())
