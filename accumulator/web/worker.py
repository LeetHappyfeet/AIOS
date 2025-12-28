# accumulator/web/worker.py

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

    def start(self):
        t = threading.Thread(target=self.run, daemon=True)
        t.start()

    def stop(self):
        self._stop.set()
        self.acc.shutdown()

    def run(self):
        while not self._stop.is_set():
            task = self.queue.pop()
            if not task:
                time.sleep(0.5)
                continue

            try:
                print(f"[worker] fetching {task.url}")
                self.acc.accumulate(task.url)
                print(f"[worker] done {task.url}")
            except Exception:
                print("[worker] ERROR")
                traceback.print_exc()
