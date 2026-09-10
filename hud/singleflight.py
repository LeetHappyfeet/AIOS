from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Generic, Hashable, TypeVar


K = TypeVar("K", bound=Hashable)
T = TypeVar("T")


class AsyncSingleFlight(Generic[K, T]):
    """Coalesce concurrent async work that has the same immutable key.

    Each waiter is shielded from the underlying task so a disconnected or
    cancelled client cannot cancel work that another caller is already sharing.
    Completed tasks remove themselves from the in-flight map automatically.
    """

    def __init__(self) -> None:
        self._tasks: dict[K, asyncio.Task[T]] = {}
        self._lock = asyncio.Lock()

    async def run(self, key: K, factory: Callable[[], Awaitable[T]]) -> T:
        async with self._lock:
            task = self._tasks.get(key)
            if task is None:
                task = asyncio.create_task(factory())
                self._tasks[key] = task
                task.add_done_callback(
                    lambda finished, flight_key=key: self._discard(flight_key, finished)
                )
        return await asyncio.shield(task)

    def _discard(self, key: K, task: asyncio.Task[T]) -> None:
        if self._tasks.get(key) is task:
            self._tasks.pop(key, None)

    def active(self) -> int:
        """Return the number of currently shared operations."""
        return len(self._tasks)
