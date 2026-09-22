from __future__ import annotations

import threading
import time
import traceback

from .queue import CrawlQueue
from .accumulator import WebAccumulator


class AccumulatorWorker:
    def __init__(self, queue: CrawlQueue):
        self.queue = queue
        self.acc = WebAccumulator()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self.run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.acc.shutdown()

    def run(self):
        while not self._stop.is_set():
            task = self.queue.pop()
            if not task:
                time.sleep(0.5)
                continue

            self.queue.update(
                task.task_id,
                state="running",
                current_url=task.url,
                message="Fetching web content",
            )

            def progress(**changes):
                self.queue.update(task.task_id, **changes)

            try:
                result = self.acc.accumulate_task(task, on_progress=progress)
                state = "completed" if result["pages_written"] > 0 else "failed"
                failures = result.get("failures") or []
                if failures:
                    first = failures[0]
                    detail = first.get("detail") or {}
                    attempts = detail.get("fetch_attempts") or []
                    attempt_text = "; ".join(
                        (
                            f"{attempt.get('method')}: "
                            f"{attempt.get('error_type') or 'failed'}"
                            f"{' HTTP ' + str(attempt.get('status_code')) if attempt.get('status_code') else ''}"
                            f"{' - ' + str(attempt.get('error')) if attempt.get('error') else ''}"
                        )
                        for attempt in attempts
                    )
                    failure_text = (
                        f" First failure: {first.get('reason')} at {first.get('url')}."
                    )
                    if attempt_text:
                        failure_text += f" Attempts: {attempt_text}."
                    elif detail:
                        failure_text += f" Detail: {detail}."
                else:
                    failure_text = ""

                self.queue.update(
                    task.task_id,
                    state=state,
                    current_url=None,
                    pages_discovered=result["pages_discovered"],
                    pages_written=result["pages_written"],
                    pages_failed=result["pages_failed"],
                    message=(
                        f"Visited {result['pages_visited']} page(s); "
                        f"wrote {result['pages_written']} observation record(s); "
                        f"{result['pages_failed']} failed."
                        f"{failure_text}"
                    )[:1000],
                )
            except Exception as exc:
                self.queue.update(
                    task.task_id,
                    state="failed",
                    current_url=None,
                    message=str(exc)[:500],
                )
                traceback.print_exc()
