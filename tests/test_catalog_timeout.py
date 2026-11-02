import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aios_app.hud.entity_catalog import hud_catalog, EntityCatalogService


@pytest.mark.asyncio
async def test_optional_catalog_timeout_has_no_traceback(monkeypatch, caplog):
    monkeypatch.setattr(EntityCatalogService, 'character', AsyncMock(side_effect=asyncio.TimeoutError))
    with caplog.at_level(logging.INFO):
        result = await hud_catalog(object(), SimpleNamespace(instance_id='instance'), token_cap=200)
    assert result['status'] == 'unavailable' and result['reason'] == 'timeout'
    assert result['entries'] == []
    assert all(record.exc_info is None for record in caplog.records)
