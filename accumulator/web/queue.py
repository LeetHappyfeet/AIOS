from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
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

    def as_dict(self) -> dict:
        return asdict(self)


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
    """Thread-safe crawl frontier with optional durable recovery."""

    def __init__(self, state_path: Path | None = None):
        self._queue: Deque[CrawlTask] = deque()
        self._tasks: Dict[str, CrawlTask] = {}
        self._status: Dict[str, CrawlStatus] = {}
        self._lock = threading.Lock()
        self._state_path = state_path
        self._load()

    def _load(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception:
            return

        for raw in data.get("tasks", []):
            try:
                task = CrawlTask(**raw)
            except TypeError:
                continue
            self._tasks[task.task_id] = task

        for raw in data.get("status", []):
            try:
                status = CrawlStatus(**raw)
            except TypeError:
                continue
            if status.state in {"queued", "running"}:
                status.state = "queued"
                status.current_url = None
                status.message = "Recovered after accumulator/UI restart"
                task = self._tasks.get(status.task_id)
                if task:
                    self._queue.append(task)
            self._status[status.task_id] = status

    def _save_locked(self) -> None:
        if self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "tasks": [task.as_dict() for task in self._tasks.values()],
            "status": [status.as_dict() for status in self._status.values()],
        }
        tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._state_path)

    def add(self, task: CrawlTask) -> str:
        with self._lock:
            self._queue.append(task)
            self._tasks[task.task_id] = task
            self._status[task.task_id] = CrawlStatus(
                task_id=task.task_id,
                url=task.url,
                source_id=task.source_id,
            )
            self._save_locked()
            return task.task_id

    def pop(self) -> CrawlTask | None:
        with self._lock:
            if not self._queue:
                return None
            task = self._queue.popleft()
            self._save_locked()
            return task

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
            self._save_locked()

    def get(self, task_id: str) -> Optional[dict]:
        with self._lock:
            status = self._status.get(task_id)
            return status.as_dict() if status else None

    def recent(self, limit: int = 25) -> List[dict]:
        with self._lock:
            rows = list(self._status.values())
            rows.sort(key=lambda item: item.updated_at, reverse=True)
            return [item.as_dict() for item in rows[:limit]]
