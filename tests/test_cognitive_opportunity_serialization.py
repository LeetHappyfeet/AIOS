import json
from uuid import uuid4

import pytest

from aios_app.agent.opportunities import _json_dumps


def test_json_dumps_normalizes_nested_uuid_values():
    value = uuid4()
    encoded = _json_dumps({
        "goal_state": {
            "subject_id": value,
            "nested": [{"source_id": value}],
        }
    })

    decoded = json.loads(encoded)
    assert decoded["goal_state"]["subject_id"] == str(value)
    assert decoded["goal_state"]["nested"][0]["source_id"] == str(value)


def test_json_dumps_rejects_unknown_non_json_types():
    with pytest.raises(TypeError):
        _json_dumps({"bad": object()})


def test_json_dumps_normalizes_goal_support_timestamps():
    from datetime import date, datetime, timezone, timedelta
    stamp = datetime(2026, 10, 14, 1, 2, 3, 456789,
                     tzinfo=timezone(timedelta(hours=-4)))
    decoded = json.loads(_json_dumps({"knowledge_demand": {"matching": [
        {"proposition_id": uuid4(), "updated_at": stamp,
         "acquired_on": date(2026, 10, 13)}
    ]}}))
    support = decoded["knowledge_demand"]["matching"][0]
    assert support["updated_at"] == "2026-10-14T01:02:03.456789-04:00"
    assert support["acquired_on"] == "2026-10-13"


@pytest.mark.asyncio
async def test_bad_later_proposal_does_not_partially_store_batch():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from aios_app.agent.opportunities import CognitiveOpportunityService
    db = SimpleNamespace(execute_returning_row=AsyncMock())
    service = CognitiveOpportunityService(db)
    proposals = [
        {"operation_payload": {"source_id": uuid4()}, "evidence": []},
        {"operation_payload": {"unsupported": object()}, "evidence": []},
    ]
    with pytest.raises(TypeError):
        await service._persist_proposals(uuid4(), proposals)
    db.execute_returning_row.assert_not_awaited()
