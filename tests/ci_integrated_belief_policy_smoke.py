"""Fresh-baseline integration smoke for authoritative belief/source policy.

Uses a disposable PostgreSQL 16 database. No live Experiment 7 rows are
modified; the new migration deliberately leaves existing beliefs untouched.
"""
from __future__ import annotations
import asyncio
import os
from pathlib import Path
from uuid import UUID
import asyncpg

SELECTED_MIGRATIONS = [
    "20260913_baseline_reference_data.sql",
    "20260917_character_memory_continuity.sql",
    "20260919_replay_correlated_evidence.sql",
    "20260919_serialized_belief_reconciliation.sql",
    "20260927_coalesced_character_belief_reconciliation.sql",
    "20260927_epistemic_authority_membrane.sql",
    "20260928_epistemic_authority_enforcement.sql",
    "20260930_01_semantic_topology_eligibility.sql",
    "20261001_claim_semantic_integrity.sql",
    "20261003_07_semantic_hygiene_shadow.sql",
    "20261003_08_semantic_hygiene_reconciliation.sql",
    "20261003_09_semantic_hygiene_revision_review.sql",
    "20261003_10_integrated_belief_policy_and_integrity.sql",
    "20261003_11_integrity_context_invalidation.sql",
    "20261003_12_occurrence_completion_invalidation.sql",
    "20261003_13_source_receipt_and_belief_hardening.sql",
]


async def main() -> None:
    con = await asyncpg.connect(os.environ["AIOS_DB_DSN"])
    try:
        await con.execute(Path("aios_baseline.sql").read_text(encoding="utf-8"))
        for name in SELECTED_MIGRATIONS:
            await con.execute((Path("migrations/current") / name).read_text(encoding="utf-8"))
            print("APPLIED:", name, flush=True)

        assert await con.fetchval("SELECT count(*) FROM aios.reconciliation_family_policy") == 16
        default = await con.fetchrow(
            """SELECT accept_support,decision_margin
                 FROM aios.belief_reconciliation_policy WHERE policy_key='default'"""
        )
        assert default and default["accept_support"] == 0.6 and default["decision_margin"] == 0.15
        await con.execute("SELECT aios.assert_belief_policy_configuration()")

        authority = await con.fetchval(
            "SELECT pg_get_functiondef('aios.reconcile_character_belief_atom_authority_v3(uuid,uuid)'::regprocedure)"
        )
        family = await con.fetchval(
            "SELECT pg_get_functiondef('aios.apply_character_belief_policy(uuid,uuid)'::regprocedure)"
        )
        wrapper = await con.fetchval(
            "SELECT pg_get_functiondef('aios.reconcile_character_belief_atom(uuid,uuid)'::regprocedure)"
        )
        assert "epistemic_authority_admission" in authority
        assert "semantic_acquisition_source_eligible" in authority
        assert "epistemic_authority_admission" in family
        assert "semantic_acquisition_source_eligible" in family
        assert "cognitive_evidence_instances" in family
        assert "reconcile_character_belief_atom_authority_v3" in wrapper
        assert "apply_character_belief_policy" in wrapper
        assert await con.fetchval(
            "SELECT aios.semantic_integrity_claim_current($1)", UUID(int=900)
        ) is False
        assert await con.fetchval(
            "SELECT aios.semantic_occurrence_topology_eligible($1,$2)",
            UUID(int=901), UUID(int=902),
        ) is False

        # Paragraph and non-resolved frame mutations are active invalidations.
        for trigger in (
            "trg_refresh_admission_from_integrity_receipt",
            "trg_refresh_admission_integrity_section_edit",
            "trg_refresh_admission_integrity_frame_mutation",
            "trg_zzz_current_integrity_admission",
            "trg_revisit_admission_after_occurrence_binding",
            "trg_revisit_admission_after_interpretation",
            "trg_refresh_admission_integrity_sentence_edit",
            "trg_guard_default_belief_policy",
            "trg_zzzz_skip_unchanged_semantic_admission",
        ):
            assert await con.fetchval(
                "SELECT count(*) FROM pg_trigger WHERE tgname=$1 AND NOT tgisinternal",
                trigger,
            ) == 1, trigger

        # Runtime cannot silently delete critical reference data. Separately,
        # simulate a damaged ledger to assert that a missing default fails closed.
        try:
            await con.execute("DELETE FROM aios.belief_reconciliation_policy WHERE policy_key='default'")
        except asyncpg.RaiseError as e:
            assert "Protected belief reconciliation default" in str(e)
        else:
            raise AssertionError("Default row was not protected")
        await con.execute(
            "ALTER TABLE aios.belief_reconciliation_policy DISABLE TRIGGER trg_guard_default_belief_policy"
        )
        await con.execute("DELETE FROM aios.belief_reconciliation_policy WHERE policy_key='default'")
        try:
            await con.execute("SELECT aios.assert_belief_policy_configuration()")
        except asyncpg.RaiseError as e:
            assert "missing belief_reconciliation_policy.default" in str(e)
        else:
            raise AssertionError("Missing default policy silently accepted")
        await con.execute(
            """INSERT INTO aios.belief_reconciliation_policy
               (policy_key,accept_support,decision_margin,resolver_version)
               VALUES ('default',0.6,0.15,'character-belief-v4-authority-family')"""
        )
        await con.execute(
            "ALTER TABLE aios.belief_reconciliation_policy ENABLE TRIGGER trg_guard_default_belief_policy"
        )
        # A real frame+receipt check: mismatched source invalidates current
        # eligibility; a new V4 receipt must contain the section digest.
        await con.execute(
            """INSERT INTO aios.document_section(section_id,section_path,section_order,content)
               VALUES($1,'0',0,'Alex saw Renamon.')""", UUID(int=111)
        )
        await con.execute(
            """INSERT INTO aios.extracted_sentence
               (sentence_id,section_id,sentence_index,sentence_text)
               VALUES($1,$2,0,'Alex saw Renamon.')""",
            UUID(int=112), UUID(int=111),
        )
        await con.execute(
            """INSERT INTO aios.claim_candidate(claim_id,sentence_id,raw_text,extraction_ver)
               VALUES ($1,$2,'Alex saw Renamon.','fixture')""",
            UUID(int=113), UUID(int=112),
        )
        await con.execute(
            """INSERT INTO aios.claim_semantic_frame
               (frame_id,claim_id,frame_index,subject_text,predicate_surface,
                object_text,resolved_subject,resolved_object,predicate_canonical,
                resolution_status,decomposer_version)
               VALUES($1,$2,0,'Alex','saw','Renamon','Alex','Renamon','see',
                      'resolved','semantic-frame-v2')""",
            UUID(int=114), UUID(int=113),
        )
        import hashlib, json
        digest = hashlib.sha256(b"Alex saw Renamon.").hexdigest()
        snapshot = [{"frame_id":str(UUID(int=114)),"subject":"Alex","predicate":"see",
                     "object":"Renamon","polarity":1,"modality":"asserted"}]
        # A V4 receipt without every source coordinate must NOT pass, even
        # when text and frame snapshot match (the former NULL-digest loophole).
        await con.execute(
            """INSERT INTO aios.claim_semantic_integrity
              (claim_id,revision_key,validator_version,status,source_text,frame_snapshot)
               VALUES($1,'r1','semantic-integrity-v4-source-coverage','valid',
                      'Alex saw Renamon.',$2::jsonb)""",
            UUID(int=113), json.dumps(snapshot),
        )
        assert await con.fetchval(
            "SELECT aios.semantic_integrity_claim_current($1)", UUID(int=113)
        ) is False

        # Historical V3 stays inspectable/eligible ONLY through its explicit
        # compatibility path, never by pretending it has a current V4 receipt.
        await con.execute(
            """UPDATE aios.claim_semantic_integrity
               SET validator_version='semantic-integrity-v3-fidelity'
               WHERE claim_id=$1""", UUID(int=113)
        )
        assert await con.fetchval(
            "SELECT aios.semantic_integrity_claim_current($1)", UUID(int=113)
        ) is True

        sentence_digest = hashlib.sha256(b"Alex saw Renamon.").hexdigest()
        await con.execute(
            """UPDATE aios.claim_semantic_integrity
               SET validator_version='semantic-integrity-v4-source-coverage',
                   revision_key='r2', source_section_digest=$2,
                   source_section_id=$3, source_sentence_digest=$4
               WHERE claim_id=$1""",
            UUID(int=113), digest, UUID(int=111), sentence_digest,
        )
        assert await con.fetchval(
            "SELECT aios.semantic_integrity_claim_current($1)", UUID(int=113)
        ) is True

        # Extracted-sentence edits and section-reparenting invalidate receipts
        # without changing claim_candidate.raw_text.
        await con.execute(
            "UPDATE aios.extracted_sentence SET sentence_text='Alex did not see Renamon.' WHERE sentence_id=$1",
            UUID(int=112),
        )
        assert await con.fetchval(
            "SELECT aios.semantic_integrity_claim_current($1)", UUID(int=113)
        ) is False
        await con.execute(
            "UPDATE aios.extracted_sentence SET sentence_text='Alex saw Renamon.' WHERE sentence_id=$1",
            UUID(int=112),
        )
        await con.execute(
            """INSERT INTO aios.document_section(section_id,section_path,section_order,content)
               VALUES($1,'1',1,'Alex saw Renamon.')""", UUID(int=115)
        )
        await con.execute(
            "UPDATE aios.extracted_sentence SET section_id=$1 WHERE sentence_id=$2",
            UUID(int=115), UUID(int=112),
        )
        assert await con.fetchval(
            "SELECT aios.semantic_integrity_claim_current($1)", UUID(int=113)
        ) is False
        await con.execute(
            "UPDATE aios.extracted_sentence SET section_id=$1 WHERE sentence_id=$2",
            UUID(int=111), UUID(int=112),
        )
        await con.execute(
            "UPDATE aios.document_section SET content='Alex did not see Renamon.' WHERE section_id=$1",
            UUID(int=111),
        )
        assert await con.fetchval(
            "SELECT aios.semantic_integrity_claim_current($1)", UUID(int=113)
        ) is False
        print("PASS: fresh baseline, seeded default, family authority composition, "
              "fail-closed policy, V4 source identity, legacy V3 compatibility",flush=True)
    finally:
        await con.close()


if __name__ == "__main__":
    asyncio.run(main())
