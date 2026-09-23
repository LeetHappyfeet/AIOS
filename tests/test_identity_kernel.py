from aios_app.char.identity_bootstrap import _authored_domain_declarations, _card_data, _facet_candidates
from aios_app.char.identity_kernel import _render, _json_value


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


def test_card_candidates_default_to_self_authored_identity():
    card = {
        "description": "A yellow fox-like Digimon.",
        "personality": "Reserved and pragmatic.",
        "scenario": "A temporary beach scene.",
    }
    facets = _facet_candidates(card)
    assert len(facets) == 2
    assert all(item["source_field"] != "scenario" for item in facets)
    assert {item["facet_type"] for item in facets} == {"appearance", "personality"}


def test_identity_json_values_decode_from_asyncpg_text():
    assert _json_value('"reserved"') == "reserved"
    assert _json_value('["reserved","pragmatic"]') == ["reserved", "pragmatic"]
    assert _json_value('{"value":true}') == {"value": True}
    assert _json_value("plain prose") == "plain prose"


def test_explicit_aios_domains_become_identity_facets_without_prose_inference():
    card = {
        "description": "Renamon appears in a crossover story with Shego.",
        "extensions": {
            "aios": {
                "identity": {
                    "domains": [
                        {"domain": "fiction.digimon", "relationship": "native"},
                        {"domain": "fiction.kim-possible", "relationship": "crossover"},
                    ]
                }
            }
        },
    }
    declarations = _authored_domain_declarations(card)
    assert declarations == [
        {"domain": "fiction.digimon", "relationship": "native"},
        {"domain": "fiction.kim-possible", "relationship": "crossover"},
    ]
    domains = [item for item in _facet_candidates(card) if item["facet_type"] == "domain"]
    assert [item["facet_key"] for item in domains] == ["fiction.digimon", "fiction.kim-possible"]


def test_character_names_and_description_never_create_domain_affinity():
    card = {
        "name": "Renamon",
        "description": "Renamon is a Digimon and meets Shego in Kim Possible.",
    }
    assert _authored_domain_declarations(card) == []
    assert not [item for item in _facet_candidates(card) if item["facet_type"] == "domain"]
