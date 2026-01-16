from __future__ import annotations

import subprocess
import signal
import sys
import time
from typing import List

SERVICES = [
    {
        "name": "Accumulator",
        "cmd": ["python", "-m", "aios_app.accumulator.main"],
    },
    {
        "name": "RAG",
        "cmd": ["python", "-m", "aios_app.rag.cli"],
    },
    {
        "name": "Supervisor",
        "cmd": ["python", "-m", "aios_app.supervisor"],
    },
    {
        "name": "Pipeline Runner",
        "cmd": ["python", "-m", "aios_app.runner"],
    },
    {
        "name": "UI",
        "cmd": ["python", "-m", "aios_app.ui.app"],
    },
    {
        "name": "API",
        "cmd": [
            "uvicorn",
            "aios_app.main:app",
            "--host", "0.0.0.0",
            "--port", "8000",
            "--reload",
        ],
    },
]


def main() -> None:
    processes: List[subprocess.Popen] = []

    print("🚀 Launching AIOS services...\n")

    try:
        for svc in SERVICES:
            print(f"▶ Starting {svc['name']}")
            p = subprocess.Popen(
                svc["cmd"],
                stdout=sys.stdout,
                stderr=sys.stderr,
                text=True,
            )
            processes.append(p)
            time.sleep(0.5)

        print("\n✅ All services started. Press Ctrl+C to stop.\n")

        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        print("\n🛑 Shutdown requested, stopping services...")

    finally:
        for p in processes:
            try:
                p.send_signal(signal.SIGINT)
            except Exception:
                pass

        time.sleep(2)

        for p in processes:
            if p.poll() is None:
                p.kill()

        print("✅ All services stopped.")


if __name__ == "__main__":
    main()
