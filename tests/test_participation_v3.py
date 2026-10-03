"""Cold-start scene significance is independent of bootstrapped goals/facets."""
from aios_app.agent.participation import propose_v2, propose_v3


EMPTY = {
    "names": ["Renamon"], "goals": [], "facets": [], "relationships": [],
    "truncated": {},
}


def claim(source, *, subject="renamon", predicate="go", speaker="Renamon",
          integrity="valid", obj=None):
    return {
        "canonical_text": f"{subject} | {predicate} | {obj or '_'}",
        "source_sentence": source, "subject_norm": subject,
        "predicate_norm": predicate, "object_norm": obj,
        "speaker_id": speaker, "target_character_id": None, "dag_node_id": "node",
        "semantic_integrity_status": integrity,
    }


def test_first_refusal_can_foreground_with_no_goals_or_facets():
    c = claim("And no, I'm not going anywhere.")
    assert propose_v2(c, EMPTY, recurrence=1)["population"] == "latent"
    v3 = propose_v3(c, EMPTY, recurrence=1)
    assert v3["population"] == "foreground"
    assert v3["signals"]["scene_consequence"]["category"] == "expressed_refusal"


def test_first_couch_request_and_situational_tenant_statement():
    request = propose_v3(claim("I'd like the couch.", predicate="like", obj="the couch"),
                         EMPTY, recurrence=1)
    role = propose_v3(claim("I'm a tenant who arrived to find the listing was false.",
                            predicate="identity", obj="a tenant"), EMPTY, recurrence=1)
    assert request["signals"]["scene_consequence"]["category"] == "direct_request"
    assert role["signals"]["scene_consequence"]["category"] == "situational_role_claim"


def test_incidental_tail_action_is_not_automatically_foreground():
    c = claim("Her tail swayed once, slow.", subject="her tail", predicate="sway")
    assert propose_v3(c, EMPTY, recurrence=1)["population"] != "foreground"


def test_incomplete_source_cannot_gain_foreground_from_consequence_keyword():
    c = claim("And no, I'm not going anywhere.", integrity="incomplete")
    v3 = propose_v3(c, EMPTY, recurrence=1)
    assert v3["population"] != "foreground"
    assert not v3["signals"]["scene_consequence"]["supported"]


def test_other_speaker_authorship_cannot_imply_character_adoption():
    c = claim("I don't want you to leave.", subject="george constanza",
              predicate="want", speaker="George Constanza")
    assert propose_v3(c, EMPTY, recurrence=1)["population"] != "foreground"


def test_v3_does_not_mutate_v2_signals():
    c = claim("And no, I'm not going anywhere.")
    v2 = propose_v2(c, EMPTY, recurrence=1)
    v3 = propose_v3(c, EMPTY, recurrence=1)
    assert v2["policy_version"] == "participation-shadow-v2"
    assert "scene_consequence" not in v2["signals"]
    assert v3["policy_version"] == "participation-shadow-v3-scene"
