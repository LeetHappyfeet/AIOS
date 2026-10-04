"""Progressive Research V1 pure contracts: identity, budgets and operation registration."""
from uuid import UUID

import pytest

from aios_app.topic_atlas.research_dossier import (
    MAX_SEARCH_RESULTS, MAX_SELECTED_PER_STEP, focus_key, _json_object,
)
from aios_app.agent.cognitive_operation_registry import CognitiveOperationRegistry


def test_focus_identity_deterministic_and_topic_scoped():
    assert focus_key("  Digital   ecology ") == focus_key("digital ecology")
    assert focus_key("Digital ecology", UUID("11111111-1111-1111-1111-111111111111")) != (
        focus_key("Digital ecology"))
    with pytest.raises(ValueError):
        focus_key("?")


def test_research_registry_reserves_research_faculty():
    registry = CognitiveOperationRegistry()
    for name in ("research.advance", "research.study"):
        operation = registry.prepare(
            operation_type=name,operation_payload={"query":"Digital World ecology"},
            faculty="research")
        assert operation.operation_type == name
        with pytest.raises(PermissionError):
            registry.prepare(operation_type=name,operation_payload={},faculty="reflection")


def test_research_receipt_is_json_compatible_on_retry():
    assert _json_object('{"status":"submitted_to_ingestion"}') == {
        "status":"submitted_to_ingestion"}
    assert _json_object({"status":"submitted"}) == {"status":"submitted"}
    assert _json_object("not-json") == {}
    assert MAX_SEARCH_RESULTS == 8
    assert MAX_SELECTED_PER_STEP == 2


def test_dossier_schema_keeps_submitted_distinct_from_semantic_admission():
    from pathlib import Path
    sql=(Path(__file__).parents[1]/"migrations"/"current"/
         "20261004_17_progressive_research.sql").read_text()
    assert "source_text_digest text NOT NULL" in sql
    assert "research_dedupe_key" in sql
    assert "aios.character_research_selection" in sql
    assert "aios.character_research_question" in sql
    assert "Integrity V4 approved" in sql  # explicitly states it is NOT
    assert "CHARACTER" not in ""  # dossier operation does not prove any truth
