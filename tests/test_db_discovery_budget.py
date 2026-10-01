import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

from aios_app.db import Database


class Pool:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


class Connection:
    def __init__(self):
        self.execute = AsyncMock()
        self.fetch = AsyncMock(return_value=[{"id": 1}])
        self.fetchval = AsyncMock(return_value=42)
        self.transaction_exited = False

    @asynccontextmanager
    async def transaction(self):
        try:
            yield
        finally:
            self.transaction_exited = True


def test_scalar_query_uses_pooled_connection():
    connection = Connection()
    db = Database("unused")
    db.pool = Pool(connection)
    assert asyncio.run(db.fetchval("SELECT $1", 42)) == 42
    connection.fetchval.assert_awaited_once_with("SELECT $1", 42)


def test_discovery_timeout_settings_are_local_to_query_transaction():
    connection = Connection()
    db = Database("unused")
    db.pool = Pool(connection)
    assert asyncio.run(db.fetch_bounded("SELECT $1", 1)) == [{"id": 1}]
    sql, statement_budget, lock_budget = connection.execute.call_args.args
    assert "true" in sql
    assert statement_budget == "5000ms"
    assert lock_budget == "1000ms"
    connection.fetch.assert_awaited_once_with("SELECT $1", 1, timeout=6.0)
    assert connection.transaction_exited


def test_failed_discovery_exits_transaction():
    connection = Connection()
    connection.fetch.side_effect = TimeoutError("budget exceeded")
    db = Database("unused")
    db.pool = Pool(connection)
    try:
        asyncio.run(db.fetch_bounded("SELECT 1"))
    except TimeoutError:
        pass
    else:
        raise AssertionError("query failure must reach supervisor")
    assert connection.transaction_exited
