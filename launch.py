from __future__ import annotations

import collections
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional

from aios_app.config import settings


PYTHON = sys.executable
DEFAULT_STARTUP_TIMEOUT = float(os.getenv("AIOS_STARTUP_TIMEOUT", "20"))
FAILURE_TAIL_LINES = int(os.getenv("AIOS_FAILURE_TAIL_LINES", "20"))


SERVICES = [
    {
        "name": "Accumulator",
        "cmd": [PYTHON, "-m", "aios_app.accumulator.main"],
        "required": True,
        "readiness": {"type": "log", "marker": "AIOS_READY service=accumulator"},
    },
    {
        "name": "RAG",
        "cmd": [PYTHON, "-m", "aios_app.rag.cli"],
        "required": False,
        "readiness": {"type": "log", "marker": "AIOS_READY service=rag"},
    },
    {
        "name": "Supervisor",
        "cmd": [PYTHON, "-m", "aios_app.supervisor"],
        "required": True,
        "readiness": {"type": "log", "marker": "AIOS supervisor started"},
    },
    {
        "name": "Pipeline Runner",
        "cmd": [PYTHON, "-m", "aios_app.runner"],
        "required": True,
        "readiness": {"type": "log", "marker": "Pipeline runner started"},
    },
    {
        "name": "UI",
        "cmd": [PYTHON, "-m", "aios_app.ui.app"],
        "required": False,
        "readiness": {
            "type": "tcp",
            "host": "127.0.0.1",
            "port": int(os.getenv("AIOS_UI_PORT", "7860")),
        },
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
        "readiness": {
            "type": "http",
            "url": f"http://127.0.0.1:{settings.api_port}/healthz",
        },
    },
]


@dataclass
class ServiceRuntime:
    spec: dict
    process: subprocess.Popen
    started_at: float
    state: str = "STARTING"
    ready_at: Optional[float] = None
    tail: Deque[str] = field(
        default_factory=lambda: collections.deque(maxlen=FAILURE_TAIL_LINES)
    )

    @property
    def name(self) -> str:
        return str(self.spec["name"])

    @property
    def required(self) -> bool:
        return bool(self.spec.get("required", True))


def _run_preflight() -> None:
    print("🗄️  Updating AIOS PostgreSQL schema...", flush=True)
    subprocess.run([PYTHON, "-m", "aios_app.migrate"], check=True)

    print("🔎 Checking AIOS PostgreSQL readiness...", flush=True)
    subprocess.run([PYTHON, "-m", "aios_app.db_check"], check=True)


def _format_state(name: str, state: str, detail: str = "") -> str:
    dots = "." * max(2, 31 - len(name))
    suffix = f"  {detail}" if detail else ""
    return f"  {name} {dots} {state}{suffix}"


def _stream_output(
    runtime: ServiceRuntime,
    output_queue: "queue.Queue[tuple[str, str]]",
) -> None:
    stream = runtime.process.stdout
    if stream is None:
        return

    try:
        for raw_line in iter(stream.readline, ""):
            line = raw_line.rstrip("\r\n")
            runtime.tail.append(line)
            output_queue.put((runtime.name, line))
    finally:
        stream.close()


def _probe_tcp(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


def _probe_http(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=0.5) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _readiness_probe(runtime: ServiceRuntime) -> bool:
    readiness = runtime.spec.get("readiness", {})
    probe_type = readiness.get("type")

    if probe_type == "tcp":
        return _probe_tcp(str(readiness["host"]), int(readiness["port"]))
    if probe_type == "http":
        return _probe_http(str(readiness["url"]))

    return False


def _mark_ready(runtime: ServiceRuntime) -> None:
    if runtime.state == "READY":
        return
    runtime.state = "READY"
    runtime.ready_at = time.monotonic()
    elapsed = runtime.ready_at - runtime.started_at
    print(_format_state(runtime.name, "READY", f"{elapsed:.1f}s"), flush=True)


def _drain_output(
    output_queue: "queue.Queue[tuple[str, str]]",
    runtimes: Dict[str, ServiceRuntime],
) -> None:
    while True:
        try:
            name, line = output_queue.get_nowait()
        except queue.Empty:
            break

        print(f"[{name}] {line}", flush=True)
        runtime = runtimes[name]
        readiness = runtime.spec.get("readiness", {})
        if (
            runtime.state == "STARTING"
            and readiness.get("type") == "log"
            and str(readiness.get("marker", "")) in line
        ):
            _mark_ready(runtime)


def _print_failure_tail(runtime: ServiceRuntime) -> None:
    if not runtime.tail:
        return

    print(f"\nLast output from {runtime.name}:", flush=True)
    for line in runtime.tail:
        print(f"  [{runtime.name}] {line}", flush=True)


def _terminate_all(runtimes: List[ServiceRuntime]) -> None:
    for runtime in runtimes:
        if runtime.process.poll() is not None:
            continue
        try:
            runtime.process.send_signal(signal.SIGINT)
        except Exception:
            pass

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if all(runtime.process.poll() is not None for runtime in runtimes):
            break
        time.sleep(0.05)

    for runtime in runtimes:
        if runtime.process.poll() is None:
            runtime.process.kill()


def main() -> None:
    runtimes: List[ServiceRuntime] = []
    output_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()

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

    print("\n🚀 Launching AIOS service processes...\n")

    try:
        for spec in SERVICES:
            process = subprocess.Popen(
                spec["cmd"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            runtime = ServiceRuntime(
                spec=spec,
                process=process,
                started_at=time.monotonic(),
            )
            runtimes.append(runtime)
            print(
                _format_state(runtime.name, "STARTING", f"pid={process.pid}"),
                flush=True,
            )
            threading.Thread(
                target=_stream_output,
                args=(runtime, output_queue),
                daemon=True,
                name=f"aios-launch-{runtime.name}",
            ).start()

        runtime_by_name = {runtime.name: runtime for runtime in runtimes}
        print("\nWaiting for service readiness...\n", flush=True)

        while any(runtime.state == "STARTING" for runtime in runtimes):
            _drain_output(output_queue, runtime_by_name)
            now = time.monotonic()

            for runtime in runtimes:
                if runtime.state != "STARTING":
                    continue

                code = runtime.process.poll()
                if code is not None:
                    runtime.state = "FAILED" if runtime.required else "DEGRADED"
                    print(
                        _format_state(
                            runtime.name,
                            runtime.state,
                            f"exited code={code}",
                        ),
                        flush=True,
                    )
                    if runtime.required:
                        _print_failure_tail(runtime)
                        raise RuntimeError(
                            f"{runtime.name} exited during startup with code {code}"
                        )
                    continue

                if _readiness_probe(runtime):
                    _mark_ready(runtime)
                    continue

                timeout = float(
                    runtime.spec.get("startup_timeout", DEFAULT_STARTUP_TIMEOUT)
                )
                if now - runtime.started_at >= timeout:
                    runtime.state = "FAILED" if runtime.required else "DEGRADED"
                    print(
                        _format_state(
                            runtime.name,
                            runtime.state,
                            f"readiness timeout after {timeout:.1f}s",
                        ),
                        flush=True,
                    )
                    if runtime.required:
                        _print_failure_tail(runtime)
                        raise RuntimeError(
                            f"{runtime.name} did not become ready within {timeout:.1f}s"
                        )

            time.sleep(0.05)

        _drain_output(output_queue, runtime_by_name)

        required = [runtime for runtime in runtimes if runtime.required]
        optional = [runtime for runtime in runtimes if not runtime.required]
        required_ready = sum(runtime.state == "READY" for runtime in required)
        optional_ready = sum(runtime.state == "READY" for runtime in optional)
        degraded = any(runtime.state == "DEGRADED" for runtime in optional)

        if degraded:
            print("\n✅ AIOS READY — DEGRADED")
        else:
            print("\n✅ AIOS READY")
        print(f"   Required services: {required_ready}/{len(required)} ready")
        print(f"   Optional services: {optional_ready}/{len(optional)} ready")
        print(f"   API: http://{settings.api_host}:{settings.api_port}")
        print("   Press Ctrl+C to stop.\n", flush=True)

        reported_optional_exits = {
            runtime.name for runtime in optional if runtime.state == "DEGRADED"
        }

        while True:
            _drain_output(output_queue, runtime_by_name)

            for runtime in runtimes:
                code = runtime.process.poll()
                if code is None:
                    continue

                if runtime.required:
                    _print_failure_tail(runtime)
                    raise RuntimeError(
                        f"{runtime.name} exited unexpectedly with code {code}"
                    )

                if runtime.name not in reported_optional_exits:
                    print(
                        f"⚠ Optional service {runtime.name} exited with code {code}; "
                        "core AIOS remains running.",
                        flush=True,
                    )
                    reported_optional_exits.add(runtime.name)

            time.sleep(0.1)

    except KeyboardInterrupt:
        print("\n🛑 Shutdown requested, stopping services...", flush=True)
    except Exception as exc:
        print(f"\n❌ AIOS service failure: {exc}", flush=True)

    finally:
        _terminate_all(runtimes)
        print("✅ All services stopped.", flush=True)


if __name__ == "__main__":
    main()
