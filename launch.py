from __future__ import annotations

import signal
import subprocess
import sys
import time
from typing import List

from aios_app.config import settings


PYTHON = sys.executable

SERVICES = [
    {
        "name": "Accumulator",
        "cmd": [PYTHON, "-m", "aios_app.accumulator.main"],
        "required": True,
    },
    {
        "name": "RAG",
        "cmd": [PYTHON, "-m", "aios_app.rag.cli"],
        "required": False,
    },
    {
        "name": "Supervisor",
        "cmd": [PYTHON, "-m", "aios_app.supervisor"],
        "required": True,
    },
    {
        "name": "Pipeline Runner",
        "cmd": [PYTHON, "-m", "aios_app.runner"],
        "required": True,
    },
    {
        "name": "UI",
        "cmd": [PYTHON, "-m", "aios_app.ui.app"],
        "required": False,
    },
    {
        "name": "API",
        "required": True,
        "cmd": [
            PYTHON,
            "-m",
            "uvicorn",
            "aios_app.main:app",
            "--host",
            settings.api_host,
            "--port",
            str(settings.api_port),
        ],
    },
]


def _run_preflight() -> None:
    print("🗄️  Updating AIOS PostgreSQL schema...")
    subprocess.run(
        [PYTHON, "-m", "aios_app.migrate"],
        check=True,
    )

    print("🔎 Checking AIOS PostgreSQL readiness...")
    subprocess.run(
        [PYTHON, "-m", "aios_app.db_check"],
        check=True,
    )


def main() -> None:
    processes: List[subprocess.Popen] = []

    print("🚀 AIOS startup")
    print(f"   API: http://{settings.api_host}:{settings.api_port}")
    print(f"   PostgreSQL: {settings.db_dsn}")
    print(f"   Fuseki: {settings.fuseki_base_url}")
    print()

    try:
        _run_preflight()
    except subprocess.CalledProcessError as exc:
        print(
            "\n❌ AIOS preflight failed. No services were started. "
            "Fix the database/configuration error above and run launch.py again."
        )
        raise SystemExit(exc.returncode)

    print("\n🚀 Launching AIOS services...\n")

    try:
        for svc in SERVICES:
            print(f"▶ Starting {svc['name']}")
            process = subprocess.Popen(
                svc["cmd"],
                stdout=sys.stdout,
                stderr=sys.stderr,
                text=True,
            )
            processes.append(process)
            time.sleep(0.5)

            if process.poll() is not None:
                if svc.get("required", True):
                    raise RuntimeError(
                        f"{svc['name']} exited during startup with code "
                        f"{process.returncode}"
                    )
                print(
                    f"⚠ Optional service {svc['name']} exited during startup "
                    f"with code {process.returncode}; core AIOS will continue."
                )

        print("\n✅ All services started. Press Ctrl+C to stop.\n")

        reported_optional_exits = set()
        while True:
            for svc, process in zip(SERVICES, processes):
                code = process.poll()
                if code is None:
                    continue

                if svc.get("required", True):
                    raise RuntimeError(
                        f"{svc['name']} exited unexpectedly with code {code}"
                    )

                if svc["name"] not in reported_optional_exits:
                    print(
                        f"⚠ Optional service {svc['name']} exited with code {code}; "
                        "core AIOS remains running."
                    )
                    reported_optional_exits.add(svc["name"])
            time.sleep(1)

    except KeyboardInterrupt:
        print("\n🛑 Shutdown requested, stopping services...")
    except Exception as exc:
        print(f"\n❌ AIOS service failure: {exc}")

    finally:
        for process in processes:
            try:
                process.send_signal(signal.SIGINT)
            except Exception:
                pass

        time.sleep(2)

        for process in processes:
            if process.poll() is None:
                process.kill()

        print("✅ All services stopped.")


if __name__ == "__main__":
    main()
