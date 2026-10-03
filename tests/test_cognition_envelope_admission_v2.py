"""Generic regressions for opt-in response adaptation and source-bound admission."""
import json

import pytest

from aios_app.inference.protocol import (
    COGNITION_UNITS_ENVELOPE, StructuredResponseError,
    extract_inference_payload, extract_json_object,
)
from aios_app.epistemic.message_cognition_enrichment import (
    review_candidate_source, ENRICHMENT_ADMISSION_VERSION,
)

SCHEMA = {"type":"object","properties":{"units":{"type":"array"}},
          "x-aios-envelope":COGNITION_UNITS_ENVELOPE}


def test_cognition_bare_array_normalized_and_original_text_remains_available():
    raw='[{"kind":"BELIEF","text":"I assumed the room was vacant","source_index":0}]'
    result, envelope=extract_inference_payload(raw,output_schema=SCHEMA)
    assert envelope=="bare_units_array_normalized"
    assert result["units"][0]["kind"]=="BELIEF"
    assert raw.startswith("[")
    with pytest.raises(StructuredResponseError,match="root must be a JSON object"):
        extract_json_object(raw)
    with pytest.raises(StructuredResponseError):
        extract_inference_payload(raw,output_schema={"type":"object"})


def test_existing_object_passes_without_rewriting():
    obj={"units":[{"kind":"BELIEF","text":"I assumed the room was vacant","source_index":0}]}
    assert extract_inference_payload(json.dumps(obj),output_schema=SCHEMA)==(obj,"object")


@pytest.mark.parametrize("raw", [
    '"text"', '5', 'null', '[2]', '[["nested"]]',
    '[{}, {}, {}, {}, {}]', '{"units":42}', '{"actions":[]}',
])
def test_no_broad_coercion_of_invalid_cognition_response(raw):
    with pytest.raises(StructuredResponseError):
        extract_inference_payload(raw,output_schema=SCHEMA)


def test_actual_failed_provider_response_one_preserves_belief_but_not_request_goal():
    excerpt=("Authentic experience. I assumed authentic meant a few roaches, "
             "perhaps, not a tenant. I'm going to need the couch.")
    belief={"kind":"BELIEF","text":"I assumed authentic meant roaches, not a tenant",
            "source_index":0}
    request={"kind":"GOAL","text":"I'm going to need the couch.",
             "objective":"get the couch","intent_type":"objective",
             "horizon":"immediate","source_index":0}
    assert review_candidate_source(belief,excerpt) is None
    assert review_candidate_source(request,excerpt)=="immediate_request_not_managed_goal"


def test_elliptical_deal_cannot_expand_prior_speaker_offer_into_adopted_goal():
    offer="Speaker_B: Stay two nights, sleep, and don't mention the email."
    item={"kind":"GOAL","text":"Deal","intent_type":"commitment",
          "horizon":"session","objective":"stay two nights, sleep, not mention email"}
    assert review_candidate_source(item,'"Deal," she said.',parent_context=offer)==(
        "elliptical_agreement_requires_explicit_adoption"
    )


def test_refusal_or_avoidance_not_turned_into_positive_task():
    item={"kind":"GOAL","text":"I'm not going to stand and lie to your mother",
          "objective":"not lie to her","horizon":"scene"}
    assert review_candidate_source(
        item,"I don't need to announce it, but I'm not going to stand here "
             "and lie to your mother on your behalf.")=="refusal_or_avoidance_not_positive_goal"
    preference={"kind":"GOAL","text":"I'd rather not spend the evening explaining",
                "objective":"avoid explaining the term","horizon":"scene"}
    assert review_candidate_source(
        preference,"I'd rather not spend the evening trying to teach you one.")==(
        "refusal_or_avoidance_not_positive_goal"
    )


def test_explicit_owned_action_remains_eligible():
    item={"kind":"GOAL","text":"I'll repair the receiver tomorrow",
          "objective":"repair the receiver tomorrow",
          "horizon":"session","intent_type":"commitment"}
    assert review_candidate_source(item,"I'll repair the receiver tomorrow.") is None
    assert ENRICHMENT_ADMISSION_VERSION=="bounded-enrichment-admission-v2-source"
