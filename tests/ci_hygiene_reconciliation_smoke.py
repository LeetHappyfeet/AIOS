"""PostgreSQL smoke: one defective source occurrence, ten inherited Mia beliefs.

Run against a disposable Postgres 16 database; never point at AIOS production.
Exercises the actual additive migration and admission trigger. The independently
unit-tested serialized belief materializer drains the resulting dirty set.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from uuid import UUID

import asyncpg

CLAIM = UUID("211137e7-673e-46d2-895b-0b31007f3ae1")
FRAME = UUID("2456f078-85a2-446d-b35f-f8bb945cb2e6")
PROP = UUID("c7f7a9bd-e7f1-4374-93d6-0c34ace5d6a2")
ATOM = UUID("16432edb-9565-468d-ba9d-75da96c572f0")
SOURCE = "You and Mia keep circling back to what I *should* know and what I *shouldn't."
INSTANCES = [UUID(int=1000 + i) for i in range(10)]

BOOTSTRAP = r'''
CREATE SCHEMA aios;
CREATE TABLE aios.claim_candidate (
 claim_id uuid PRIMARY KEY, raw_text text NOT NULL
);
CREATE TABLE aios.claim_semantic_frame (
 frame_id uuid PRIMARY KEY,claim_id uuid NOT NULL REFERENCES aios.claim_candidate,
 resolution_status text NOT NULL
);
CREATE TABLE aios.proposition (
 proposition_id uuid PRIMARY KEY,atom_id uuid NOT NULL
);
CREATE TABLE aios.observation (
 observation_id uuid PRIMARY KEY,claim_id uuid NOT NULL REFERENCES aios.claim_candidate,
 proposition_id uuid NOT NULL REFERENCES aios.proposition
);
CREATE TABLE aios.observation_proposition (
 observation_id uuid NOT NULL REFERENCES aios.observation,
 proposition_id uuid NOT NULL REFERENCES aios.proposition,
 frame_id uuid NOT NULL REFERENCES aios.claim_semantic_frame
);
CREATE TABLE aios.proposition_evidence (
 evidence_id uuid PRIMARY KEY,observation_id uuid NOT NULL REFERENCES aios.observation,
 proposition_id uuid NOT NULL REFERENCES aios.proposition
);
CREATE TABLE aios.semantic_interpretation (
 claim_id uuid NOT NULL REFERENCES aios.claim_candidate,
 frame_id uuid NOT NULL REFERENCES aios.claim_semantic_frame,
 standalone_semantic boolean NOT NULL
);
CREATE TABLE aios.character_instance (
 instance_id uuid PRIMARY KEY,parent_instance_id uuid REFERENCES aios.character_instance,
 character_id text NOT NULL
);
CREATE TABLE aios.knowledge_acquisition_event (
 acquisition_id uuid PRIMARY KEY,claim_id uuid REFERENCES aios.claim_candidate,
 proposition_id uuid REFERENCES aios.proposition,
 instance_id uuid NOT NULL REFERENCES aios.character_instance
);
CREATE TABLE aios.semantic_evidence_admission (
 acquisition_id uuid PRIMARY KEY REFERENCES aios.knowledge_acquisition_event,
 status text NOT NULL,reason text NOT NULL,confidence double precision NOT NULL,
 resolver_version text NOT NULL DEFAULT 'fixture',
 meta jsonb NOT NULL DEFAULT '{}'::jsonb,
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE aios.character_belief_state (
 instance_id uuid NOT NULL REFERENCES aios.character_instance,
 atom_id uuid NOT NULL,positive_support double precision NOT NULL,
 preferred_proposition_id uuid,PRIMARY KEY (instance_id,atom_id)
);
CREATE TABLE aios.character_belief_reconciliation_dirty (
 instance_id uuid NOT NULL REFERENCES aios.character_instance,
 atom_id uuid NOT NULL,dirty_version bigint NOT NULL DEFAULT 1,
 dirty_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (instance_id,atom_id)
);
CREATE TABLE aios.semantic_topology_node (
 topology_node_id uuid PRIMARY KEY,scope_key text NOT NULL,proposition_id uuid,
 node_type text NOT NULL,node_key text NOT NULL
);
CREATE FUNCTION aios.recompute_semantic_evidence_admission(p_acquisition uuid)
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
 UPDATE aios.semantic_evidence_admission
 SET status='active',reason='recomputed',confidence=0.6,updated_at=now()
 WHERE acquisition_id=p_acquisition;
END $$;
CREATE TABLE aios.semantic_scope_projection_state (
 scope_key text PRIMARY KEY,dirty_version bigint NOT NULL DEFAULT 0,
 projected_version bigint NOT NULL DEFAULT 0,status text NOT NULL DEFAULT 'ready',
 dirty_at timestamptz,first_dirty_at timestamptz,last_error text,
 updated_at timestamptz NOT NULL DEFAULT now()
);
-- Match the production ancestor-invalidation behavior without inventing any
-- new belief scoring or mutating existing belief state synchronously.
CREATE FUNCTION aios.mark_character_belief_dirty_descendants(
 p_source_instance_id uuid,p_atom_id uuid
) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
 INSERT INTO aios.character_belief_reconciliation_dirty
   (instance_id,atom_id,dirty_version,dirty_at)
 WITH RECURSIVE descendants AS (
    SELECT instance_id FROM aios.character_instance WHERE instance_id=p_source_instance_id
    UNION ALL
    SELECT child.instance_id FROM aios.character_instance child
    JOIN descendants parent ON child.parent_instance_id=parent.instance_id
 )
 SELECT instance_id,p_atom_id,1,now() FROM descendants
 ON CONFLICT (instance_id,atom_id) DO UPDATE
 SET dirty_version=aios.character_belief_reconciliation_dirty.dirty_version+1,
     dirty_at=now();
END $$;
'''


async def main() -> None:
    dsn = os.environ["AIOS_DB_DSN"]
    con = await asyncpg.connect(dsn)
    try:
        await con.execute(BOOTSTRAP)
        for name in (
            "20261001_claim_semantic_integrity.sql",
            "20261003_07_semantic_hygiene_shadow.sql",
            "20261003_08_semantic_hygiene_reconciliation.sql",
            "20261003_09_semantic_hygiene_revision_review.sql",
        ):
            await con.execute(
                (Path("migrations/current") / name).read_text(encoding="utf-8")
            )

        obs, evid, acquisition = UUID(int=201), UUID(int=202), UUID(int=203)
        revision, validator = "rev-mia-fixture", "semantic-integrity-v3-fidelity"
        await con.execute(
            "INSERT INTO aios.claim_candidate VALUES($1,$2)", CLAIM, SOURCE
        )
        await con.execute(
            "INSERT INTO aios.claim_semantic_frame VALUES($1,$2,'resolved')", FRAME, CLAIM
        )
        await con.execute("INSERT INTO aios.proposition VALUES($1,$2)", PROP, ATOM)
        await con.execute("INSERT INTO aios.observation VALUES($1,$2,$3)", obs, CLAIM, PROP)
        await con.execute(
            "INSERT INTO aios.observation_proposition VALUES($1,$2,$3)", obs, PROP, FRAME
        )
        await con.execute(
            "INSERT INTO aios.proposition_evidence VALUES($1,$2,$3)", evid, obs, PROP
        )
        await con.execute(
            "INSERT INTO aios.semantic_interpretation VALUES($1,$2,true)", CLAIM, FRAME
        )
        await con.execute(
            """INSERT INTO aios.claim_semantic_integrity
                 (claim_id,revision_key,validator_version,status,source_text,frame_snapshot)
               VALUES($1,$2,$3,'valid',$4,'[]'::jsonb)""",
            CLAIM, revision, validator, SOURCE,
        )
        for i, instance in enumerate(INSTANCES):
            await con.execute(
                "INSERT INTO aios.character_instance VALUES($1,$2,'Renamon')",
                instance, INSTANCES[i-1] if i else None,
            )
            await con.execute(
                "INSERT INTO aios.character_belief_state VALUES($1,$2,$3,$4)",
                instance, ATOM, 0.6013332461412312, PROP,
            )
            if i != 4:  # The historical example has one missing topology node.
                await con.execute(
                    """INSERT INTO aios.semantic_topology_node
                         VALUES($1,'char:Renamon',$2,'BELIEF_STATE',$3)""",
                    UUID(int=300+i), PROP,
                    f"belief:{instance}:{ATOM}",
                )
        await con.execute(
            "INSERT INTO aios.knowledge_acquisition_event VALUES($1,$2,$3,$4)",
            acquisition, CLAIM, PROP, INSTANCES[0],
        )
        await con.execute(
            """INSERT INTO aios.semantic_evidence_admission
                 (acquisition_id,status,reason,confidence)
               VALUES($1,'active','admitted',0.6013332461412312)""",
            acquisition,
        )
        await con.execute(
            """INSERT INTO aios.semantic_scope_projection_state
               (scope_key,dirty_version,projected_version,status)
               VALUES ('char:Renamon',2,2,'ready')"""
        )
        source_snapshot = [{
            "claim_id": str(CLAIM), "frame_id": str(FRAME),
            "proposition_id": str(PROP), "revision_key": revision,
            "validator_version": validator, "raw_text": SOURCE,
        }]
        audit = await con.fetchval(
            """INSERT INTO aios.semantic_hygiene_shadow_audit
               (atom_id,population,policy_version,source_signature,
                disposition,reason_codes,source_claim_ids,source_revision_keys,
                evidence_snapshot)
               VALUES ($1,'v3_only','semantic-hygiene-shadow-v1','mia-regression',
                       'repair_candidate',
                       '["suspect_subject_boundary","missing_expected_argument"]'::jsonb,
                       $2::jsonb,$3::jsonb,$4::jsonb)
               RETURNING audit_id""",
            ATOM, json.dumps([str(CLAIM)]), json.dumps([revision]),
            json.dumps({"atom": {"subject": "mia and", "predicate": "keep", "object": None},
                        "sources": source_snapshot}),
        )
        assert await con.fetchval(
            "SELECT aios.semantic_occurrence_topology_eligible($1,$2)", CLAIM, PROP
        ) is True

        proposal = await con.fetchval(
            "SELECT aios.propose_semantic_hygiene_adjudication($1,$2,$3,$4,$5)",
            audit, CLAIM, FRAME, PROP, "suppress_independent",
        )
        assert proposal == await con.fetchval(
            "SELECT aios.propose_semantic_hygiene_adjudication($1,$2,$3,$4,$5)",
            audit, CLAIM, FRAME, PROP, "suppress_independent",
        )
        assert await con.fetchval(
            "SELECT status FROM aios.semantic_evidence_admission WHERE acquisition_id=$1",
            acquisition,
        ) == "active"  # Proposal is inert.

        try:
            await con.fetchval(
                "SELECT aios.apply_semantic_hygiene_adjudication($1,'test')", proposal
            )
        except asyncpg.PostgresError as exc:
            assert "disabled" in str(exc)
        else:
            raise AssertionError("Ungated production mutation was allowed")

        async with con.transaction():
            await con.execute(
                "SELECT set_config('aios.semantic_hygiene_apply_enabled','on',true)"
            )
            result = await con.fetchval(
                "SELECT aios.apply_semantic_hygiene_adjudication($1,'fixture-operator')",
                proposal,
            )
            if isinstance(result,str): result=json.loads(result)
            assert result["raw_evidence_preserved"] is True
            assert result["belief_coordinates_dirtied"] == 10
            assert await con.fetchval(
                "SELECT aios.apply_semantic_hygiene_adjudication($1,'fixture-operator')",
                proposal,
            ) is not None  # idempotent; second call has no new effects.

        assert await con.fetchval(
            "SELECT aios.semantic_occurrence_topology_eligible($1,$2)", CLAIM, PROP
        ) is False
        assert await con.fetchval(
            "SELECT aios.semantic_proposition_topology_eligible($1)", PROP
        ) is False
        assert await con.fetchval(
            "SELECT count(*) FROM aios.character_belief_reconciliation_dirty WHERE atom_id=$1",
            ATOM,
        ) == 10
        assert await con.fetchval(
            "SELECT count(*) FROM aios.semantic_hygiene_adjudication_event"
        ) == 1
        assert await con.fetchval(
            "SELECT dirty_version FROM aios.semantic_scope_projection_state WHERE scope_key='char:Renamon'"
        ) > 2

        # A stale recompute cannot recreate live support.
        await con.execute(
            """UPDATE aios.semantic_evidence_admission
               SET status='active',reason='admitted',confidence=0.9
               WHERE acquisition_id=$1""", acquisition
        )
        assert await con.fetchval(
            "SELECT status FROM aios.semantic_evidence_admission WHERE acquisition_id=$1",
            acquisition,
        ) == "suppressed"
        assert await con.fetchval(
            "SELECT confidence FROM aios.semantic_evidence_admission WHERE acquisition_id=$1",
            acquisition,
        ) == 0
        # The one source and its historical evidence remain unchanged.
        assert await con.fetchval("SELECT count(*) FROM aios.claim_candidate") == 1
        assert await con.fetchval("SELECT count(*) FROM aios.observation") == 1
        assert await con.fetchval("SELECT count(*) FROM aios.proposition_evidence") == 1
        assert await con.fetchval("SELECT count(*) FROM aios.knowledge_acquisition_event") == 1
        assert await con.fetchval("SELECT count(*) FROM aios.character_belief_state") == 10

        # Separately supported occurrence of the shared proposition survives.
        other_claim, other_frame, other_obs = UUID(int=501),UUID(int=502),UUID(int=503)
        await con.execute("INSERT INTO aios.claim_candidate VALUES($1,$2)",
                          other_claim,"Mia kept the paper.")
        await con.execute("INSERT INTO aios.claim_semantic_frame VALUES($1,$2,'resolved')",
                          other_frame,other_claim)
        await con.execute("INSERT INTO aios.observation VALUES($1,$2,$3)",
                          other_obs,other_claim,PROP)
        await con.execute("INSERT INTO aios.observation_proposition VALUES($1,$2,$3)",
                          other_obs,PROP,other_frame)
        await con.execute("INSERT INTO aios.semantic_interpretation VALUES($1,$2,true)",
                          other_claim,other_frame)
        assert await con.fetchval(
            "SELECT aios.semantic_occurrence_topology_eligible($1,$2)",other_claim,PROP
        ) is True
        assert await con.fetchval(
            "SELECT aios.semantic_proposition_topology_eligible($1)",PROP
        ) is True
        # A new integrity revision cannot auto-reactivate the old rejection.
        await con.execute(
            """UPDATE aios.claim_semantic_integrity
               SET revision_key='rev-mia-corrected',checked_at=now()
               WHERE claim_id=$1""", CLAIM
        )
        assert await con.fetchval(
            "SELECT aios.semantic_occurrence_topology_eligible($1,$2)", CLAIM, PROP
        ) is False
        await con.execute(
            """UPDATE aios.semantic_evidence_admission
               SET status='active',reason='recomputed',confidence=0.8
               WHERE acquisition_id=$1""", acquisition
        )
        assert await con.fetchval(
            "SELECT reason FROM aios.semantic_evidence_admission WHERE acquisition_id=$1",
            acquisition,
        ) == "hygiene_revision_requires_review"

        # Release only after independently revalidating the revised source.
        async with con.transaction():
            await con.execute(
                "SELECT set_config('aios.semantic_hygiene_apply_enabled','on',true)"
            )
            result = await con.fetchval(
                "SELECT aios.supersede_semantic_hygiene_adjudication($1,'fixture-operator')",
                proposal,
            )
            if isinstance(result,str): result=json.loads(result)
            assert result["status"] == "superseded"
        assert await con.fetchval(
            "SELECT status FROM aios.semantic_evidence_admission WHERE acquisition_id=$1",
            acquisition,
        ) == "active"
        assert await con.fetchval(
            "SELECT aios.semantic_occurrence_topology_eligible($1,$2)", CLAIM, PROP
        ) is True

        # Old revision is an immutable tombstone: restoring the historical
        # same erroneous extraction must not reintroduce it as active evidence.
        await con.execute(
            "UPDATE aios.claim_semantic_integrity SET revision_key=$2 WHERE claim_id=$1",
            CLAIM, revision,
        )
        await con.execute(
            """UPDATE aios.semantic_evidence_admission
               SET status='active',reason='recomputed',confidence=0.8
               WHERE acquisition_id=$1""", acquisition
        )
        assert await con.fetchval(
            "SELECT status FROM aios.semantic_evidence_admission WHERE acquisition_id=$1",
            acquisition,
        ) == "suppressed"
        assert await con.fetchval(
            "SELECT aios.semantic_occurrence_topology_eligible($1,$2)", CLAIM, PROP
        ) is False
        print("PASS: source-bound suppression, 10 descendant invalidations, "
              "historical preservation, revision review/release, replay guard and independent support")
    finally:
        await con.close()


if __name__ == "__main__":
    asyncio.run(main())
