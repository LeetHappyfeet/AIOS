from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from aios_app.ingest_sync import IngestSyncIn, validate_ingest_receipts


def body(**changes):
    data = dict(session_id=str(uuid4()), character_id='Alex', user_name='Mia', receipts=[])
    data.update(changes)
    return data


def receipt(**changes):
    data = dict(message_id=0, event_id=1, node_id=str(uuid4()), speaker_id='Alex',
                speaker_type='character', text_hash='a'*64)
    data.update(changes)
    return data


@pytest.mark.parametrize('receipts', [[receipt()]*257, [receipt(), receipt()],
    [receipt(text_hash='bad')], [receipt(event_id=2**64)], [receipt(message_id=-1)],
    [receipt(text='do not send message bodies')]])
def test_manifest_bounds_and_unknown_body_fields(receipts):
    with pytest.raises(ValidationError):
        IngestSyncIn(**body(receipts=receipts))


@pytest.mark.asyncio
async def test_unknown_instance_and_foreign_binding_are_explicit():
    db = AsyncMock()
    db.fetch_bounded.return_value = []
    with pytest.raises(HTTPException) as exc:
        await validate_ingest_receipts(db, uuid4(), IngestSyncIn(**body()))
    assert exc.value.status_code == 404
    db.fetch_bounded.return_value = [{'mapping_matches': False}]
    with pytest.raises(HTTPException) as exc:
        await validate_ingest_receipts(db, uuid4(), IngestSyncIn(**body()))
    assert exc.value.status_code == 409
    assert db.fetch_bounded.call_args.kwargs['timeout_seconds'] == 2.0
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_timeout_cannot_be_mistaken_for_success():
    db = AsyncMock()
    db.fetch_bounded.side_effect = TimeoutError()
    with pytest.raises(HTTPException) as exc:
        await validate_ingest_receipts(db, uuid4(), IngestSyncIn(**body()))
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_public_sync_route_validates_body_and_uses_the_service(monkeypatch):
    import aios_app.main as main
    service = AsyncMock(return_value={'protocol': 'ingest-sync-v1', 'replay_required': False})
    monkeypatch.setattr(main, 'validate_ingest_receipts', service)
    instance = uuid4()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://aios') as client:
        invalid = await client.post(f'/instance/{instance}/sync', json=body(receipts=[receipt(text_hash='bad')]))
        assert invalid.status_code == 422
        service.assert_not_called()
        result = await client.post(f'/instance/{instance}/sync', json=body())
        assert result.status_code == 200 and result.json()['protocol'] == 'ingest-sync-v1'
        assert service.call_args.args[1] == instance
