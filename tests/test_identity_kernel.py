from aios_app.char.identity_bootstrap import _card_data, _facet_candidates
from aios_app.char.identity_kernel import _render


def test_character_card_bootstrap_routes_only_identity_fields():
    payload = {
        "spec": "chara_card_v2",
        "data": {
            "name": "Renamon",
            "description": "A yellow fox-like Digimon.",
            "personality": "Reserved, pragmatic, and independent.",
            "scenario": "Renamon has just arrived at the beach.",
            "first_mes": "The beach?",
            "mes_example": "{{char}}: I prefer practical answers.",
        },
    }
    card = _card_data(payload)
    facets = _facet_candidates(card)
    fields = {item["source_field"] for item in facets}

    assert fields == {"description", "personality", "mes_example"}
    assert "scenario" not in fields
    assert "first_mes" not in fields
    assert next(x for x in facets if x["source_field"] == "description")["stability"] == "constitutional"
    assert next(x for x in facets if x["source_field"] == "personality")["stability"] == "core"


def test_kernel_is_deterministic_and_keeps_legacy_baseline():
    identity = {
        "character_id": "Renamon",
        "canonical_name": "Renamon",
        "display_name": "Renamon",
        "entity_type": "character",
        "species": "Digimon",
        "visual_summary": "Yellow fox-like Digimon.",
        "speech_style": "Composed and concise.",
    }
    facets = [
        {
            "facet_type": "personality",
            "facet_key": "description",
            "value": "Reserved and pragmatic.",
            "stability": "core",
            "authority": "authored",
        },
        {
            "facet_type": "appearance",
            "facet_key": "description",
            "value": "Yellow fur with purple markings.",
            "stability": "constitutional",
            "authority": "authored",
        },
    ]

    payload_a, text_a = _render(identity, facets)
    payload_b, text_b = _render(identity, list(reversed(facets)))

    assert payload_a == payload_b
    assert text_a == text_b
    assert "Species: Digimon" in text_a
    assert "AUTHORED BASELINE:" in text_a
    assert "Reserved and pragmatic." in text_a
