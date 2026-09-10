import asyncio

import pytest

from aios_app.hud.singleflight import AsyncSingleFlight


@pytest.mark.asyncio
async def test_singleflight_coalesces_same_key():
    flight = AsyncSingleFlight[str, int]()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def work() -> int:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return 42

    first = asyncio.create_task(flight.run("same", work))
    await started.wait()
    second = asyncio.create_task(flight.run("same", work))
    await asyncio.sleep(0)

    assert calls == 1
    assert flight.active() == 1

    release.set()
    assert await first == 42
    assert await second == 42
    await asyncio.sleep(0)
    assert flight.active() == 0


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_shared_work():
    flight = AsyncSingleFlight[str, str]()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def work() -> str:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return "ready"

    first = asyncio.create_task(flight.run("coordinate", work))
    await started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    second = asyncio.create_task(flight.run("coordinate", work))
    await asyncio.sleep(0)
    assert calls == 1

    release.set()
    assert await second == "ready"
