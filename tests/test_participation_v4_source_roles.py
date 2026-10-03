"""Source role separation and historical-context abstention are shadow-only."""
from aios_app.agent.participation import propose, propose_v2, propose_v3, propose_v4

CONTEXT = {
    "names":["Character_A"],"goals":[],"facets":[],"relationships":[],
    "truncated":{},"coverage":{"temporal_basis":"live_head_snapshot"},
}

def claim(source, *, subject="Character_A", speaker="Character_A",
          predicate="leave", obj=None, integrity="valid"):
    return {"canonical_text":f"{subject} | {predicate} | {obj or '_'}",
        "subject_norm":subject.casefold(),"object_norm":obj,
        "predicate_norm":predicate,"source_sentence":source,
        "speaker_id":speaker,"target_character_id":None,
        "semantic_integrity_status":integrity,"dag_node_id":"node"}


def test_narrating_environment_does_not_mean_character_involved():
    c=claim("The lamps are bright.",subject="the lamps",predicate="be",
            obj="bright")
    assert propose(c,CONTEXT)["population"] == "foreground"  # frozen V1
    v4=propose_v4(c,CONTEXT,recurrence=1)
    assert v4["population"] == "latent"
    assert v4["signals"]["source_roles"]["narrator"] is True
    assert v4["signals"]["source_roles"]["actor"] is False
    assert v4["signals"]["involvement"]["source_authorship_only"] is True


def test_self_asserted_refusal_is_scene_consequence_not_new_task():
    v4=propose_v4(claim("I won't leave."),CONTEXT,recurrence=1)
    assert v4["population"] == "foreground"
    assert v4["signals"]["scene_consequence"]["category"] == "expressed_refusal"
    assert v4["signals"]["scene_consequence"]["adoption_status"] == "self_asserted"


def test_external_proposal_is_not_character_adoption():
    c=claim("You're giving me thirty seconds.",
            speaker="Speaker_B",predicate="give",obj="thirty seconds")
    v4=propose_v4(c,CONTEXT,recurrence=1)
    assert v4["population"] == "foreground"
    assert v4["signals"]["scene_consequence"]["category"] == "addressed_proposal"
    assert v4["signals"]["scene_consequence"]["adoption_status"] == "unconfirmed"
    assert v4["signals"]["source_roles"]["adopter"] is None


def test_invalid_or_incomplete_frame_cannot_be_foregrounded_by_keywords():
    for state in ("invalid","incomplete","unknown"):
        v4=propose_v4(claim("I won't leave.",integrity=state),CONTEXT,recurrence=1)
        assert v4["population"] != "foreground"
        assert not v4["signals"]["scene_consequence"]["supported"]


def test_current_character_goals_not_assumed_present_in_historical_past():
    ctx={**CONTEXT,
         "coverage":{"temporal_basis":"retrospective_snapshot_unverified"},
         "goals":[{"goal_id":"goal","goal_text":"repair the receiver now"}]}
    c=claim("Character_A repaired the receiver.",predicate="repair",
            obj="receiver",speaker="Speaker_B")
    v4=propose_v4(c,ctx,recurrence=1)
    assert v4["signals"]["goal_ids"] == []
    assert "historical_relevance_not_reconstructible" in v4["signals"]["unknown"]
    assert v4["population"] != "foreground"


def test_legacy_experiment_snapshot_is_not_mistaken_for_live_context():
    ctx={**CONTEXT,
         "coverage":{"temporal_basis":"evaluation_time_not_historical"},
         "goals":[{"goal_id":"future_goal","goal_text":"repair the receiver"}]}
    c=claim("The receiver was repaired.",subject="the receiver",
            predicate="repair",speaker="Speaker_B")
    v4=propose_v4(c,ctx,recurrence=1)
    assert v4["signals"]["goal_ids"] == []
    assert "historical_relevance_not_reconstructible" in v4["signals"]["unknown"]


def test_v4_is_additive_and_does_not_mutate_v1_v2_v3():
    c=claim("The lamps are bright.",subject="the lamps",predicate="be")
    before=(propose(c,CONTEXT),propose_v2(c,CONTEXT),
            propose_v3(c,CONTEXT))
    propose_v4(c,CONTEXT)
    assert before == (propose(c,CONTEXT),propose_v2(c,CONTEXT),
                      propose_v3(c,CONTEXT))
