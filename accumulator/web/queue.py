# accumulator/web/queue.py

from dataclasses import dataclass
from typing import List
import threading


@dataclass
class CrawlTask:
    url: str


class CrawlQueue:
    def __init__(self):
        self._queue: List[CrawlTask] = []
        self._lock = threading.Lock()

    def add(self, task: CrawlTask):
        with self._lock:
            self._queue.append(task)

    def pop(self) -> CrawlTask | None:
        with self._lock:
            if not self._queue:
                return None
            return self._queue.pop(0)

    def size(self) -> int:
        with self._lock:
            return len(self._queue)
