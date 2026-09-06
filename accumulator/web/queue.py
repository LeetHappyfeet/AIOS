from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import threading
from typing import Deque, Dict, List, Optional
from uuid import uuid4


@dataclass(frozen=True)
class CrawlTask:
    url: str
    source_id: str
    source_kind: str = "website"
    source_name: Optional[str] = None
    speaker_id: Optional[str] = None
    target_character_id: Optional[str] = None
    target_world_id: Optional[str] = None
    crawl_mode: str = "page"
    max_depth: int = 0
    max_pages: int = 1
    same_domain_only: bool = True
    respect_robots: bool = True
    task_id: str = field(default_factory=lambda: str(uuid4()))


@dataclass
class CrawlStatus:
    task_id: str
    url: str
    source_id: str
    state: str = "queued"
    pages_discovered: int = 0
    pages_written: int = 0
    pages_failed: int = 0
    current_url: Optional[str] = None
    message: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def as_dict(self) -> dict:
        return asdict(self)


class CrawlQueue:
    """Thread-safe in-process frontier with inspectable task status."""

    def __init__(self):
        self._queue: Deque[CrawlTask] = deque()
        self._status: Dict[str, CrawlStatus] = {}
        self._lock = threading.Lock()

    def add(self, task: CrawlTask) -> str:
        with self._lock:
            self._queue.append(task)
            self._status[task.task_id] = CrawlStatus(
                task_id=task.task_id,
                url=task.url,
                source_id=task.source_id,
            )
            return task.task_id

    def pop(self) -> CrawlTask | None:
        with self._lock:
            if not self._queue:
                return None
            return self._queue.popleft()

    def size(self) -> int:
        with self._lock:
            return len(self._queue)

    def update(self, task_id: str, **changes) -> None:
        with self._lock:
            status = self._status.get(task_id)
            if status is None:
                return
            for key, value in changes.items():
                if hasattr(status, key):
                    setattr(status, key, value)
            status.updated_at = datetime.now(timezone.utc).isoformat()

    def get(self, task_id: str) -> Optional[dict]:
        with self._lock:
            status = self._status.get(task_id)
            return status.as_dict() if status else None

    def recent(self, limit: int = 25) -> List[dict]:
        with self._lock:
            rows = list(self._status.values())
            rows.sort(key=lambda item: item.updated_at, reverse=True)
            return [item.as_dict() for item in rows[:limit]]
