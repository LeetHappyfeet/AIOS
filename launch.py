from __future__ import annotations

import collections
import json
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
from typing import Any, Deque, Dict, List, Optional

from aios_app.config import settings


PYTHON = sys.executable
DEFAULT_STARTUP_TIMEOUT = float(os.getenv("AIOS_STARTUP_TIMEOUT", "20"))
CORE_STARTUP_TIMEOUT = float(os.getenv("AIOS_CORE_STARTUP_TIMEOUT", "60"))
FAILURE_TAIL_LINES = int(os.getenv("AIOS_FAILURE_TAIL_LINES", "20"))
STATUS_INTERVAL = float(os.getenv("AIOS_STATUS_INTERVAL", "10"))
LOG_MODE = os.getenv("AIOS_LOG_MODE", "normal").strip().lower()
if LOG_MODE not in {"normal", "verbose", "debug"}:
    LOG_MODE = "normal"

_TELEMETRY: Dict[str, dict[str, Any]] = {}
_LAST_STATES: Dict[str, str] = {}


SERVICES = [
    {
        "name": "Accumulator",
        "cmd": [PYTHON, "-m", "aios_app.accumulator.main"],
        "required": True,
        "readiness": {"type": "log", "marker": "AIOS_READY service=accumulator"},
    },
    {
        "name": "Semantic Index",
        "cmd": [PYTHON, "-m", "aios_app.semantic_index.cli"],
        "required": False,
        "startup_timeout": float(os.getenv("AIOS_SEMANTIC_INDEX_STARTUP_TIMEOUT", "120")),
        "readiness": {"type": "log", "marker": "AIOS_READY service=semantic_index"},
    },
    {
        "name": "Supervisor",
        "cmd": [PYTHON, "-m", "aios_app.supervisor"],
        "required": True,
        "readiness": {"type": "log", "marker": "AIOS supervisor started"},
    },
    {
        "name": "Pipeline Runner",
        "cmd": [PYTHON, "-m", "aios_app.runner_v2"],
        "required": True,
        "startup_timeout": CORE_STARTUP_TIMEOUT,
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
        "startup_timeout": CORE_STARTUP_TIMEOUT,
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


STARTUP_STAGES = [
    (
        "Core services",
        {"Accumulator", "Supervisor", "Pipeline Runner", "API"},
    ),
    (
        "UI",
        {"UI"},
    ),
    (
        "Semantic Index",
        {"Semantic Index"},
    ),
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
    ok, _ = _probe_http_latency(url)
    return ok


def _probe_http_latency(url: str) -> tuple[bool, Optional[float]]:
    started = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=0.5) as response:
            elapsed_ms = (time.monotonic() - started) * 1000.0
            return 200 <= response.status < 300, elapsed_ms
    except (urllib.error.URLError, TimeoutError, OSError):
        return False, None


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


def _parse_telemetry(line: str) -> Optional[dict[str, Any]]:
    marker = "AIOS_TELEMETRY "
    if not line.startswith(marker):
        return None
    try:
        payload = json.loads(line[len(marker):])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _is_important_line(line: str) -> bool:
    upper = line.upper()
    return (
        " WARNING" in upper
        or "WARNING:" in upper
        or " ERROR" in upper
        or "ERROR:" in upper
        or "CRITICAL" in upper
        or "TRACEBACK" in upper
        or "EXCEPTION" in upper
        or "FAILED" in upper
    )


def _should_print_line(name: str, line: str) -> bool:
    if LOG_MODE == "debug":
        return True
    if _is_important_line(line):
        return True

    if name == "API" and ' "GET /healthz HTTP/' in line and " 200 " in line:
        return False

    if name == "Pipeline Runner":
        noisy = (
            "INFO:aios.pipeline.runner:Job done " in line
            or "INFO:aios.pipeline.runner:Scheduler queues " in line
            or "INFO:aios.epistemic.topology_projection:Projected semantic topology " in line
        )
        if noisy:
            return LOG_MODE == "verbose" and "Job done " not in line

    if name == "Semantic Index":
        if "INFO:httpx:HTTP Request:" in line or "INFO:httpcore:" in line:
            return False
        if "INFO:aios.semantic_index:Indexed " in line:
            return LOG_MODE == "verbose"

    return True


def _announce_state_transition(payload: dict[str, Any]) -> None:
    service = str(payload.get("service") or "")
    state = str(payload.get("state") or "")
    if not service or not state:
        return
    previous = _LAST_STATES.get(service)
    _LAST_STATES[service] = state
    if previous is None or previous == state:
        return
    symbol = "✓" if state in {"CAUGHT_UP", "HEALTHY"} else "⚠"
    print(f"{symbol} {service} state {previous} → {state}", flush=True)


def _drain_output(
    output_queue: "queue.Queue[tuple[str, str]]",
    runtimes: Dict[str, ServiceRuntime],
) -> None:
    while True:
        try:
            name, line = output_queue.get_nowait()
        except queue.Empty:
            break

        runtime = runtimes[name]
        readiness = runtime.spec.get("readiness", {})
        if (
            runtime.state == "STARTING"
            and readiness.get("type") == "log"
            and str(readiness.get("marker", "")) in line
        ):
            _mark_ready(runtime)

        payload = _parse_telemetry(line)
        if payload is not None:
            service = str(payload.get("service") or name)
            _TELEMETRY[service] = payload
            _announce_state_transition(payload)
            continue

        if _should_print_line(name, line):
            print(f"[{name}] {line}", flush=True)


def _format_age(seconds: Any) -> str:
    try:
        value = max(0.0, float(seconds or 0.0))
    except (TypeError, ValueError):
        return "-"
    if value < 1.0:
        return f"{value * 1000:.0f}ms"
    if value < 60.0:
        return f"{value:.1f}s"
    minutes, secs = divmod(int(value), 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _format_rate(value: Any) -> str:
    try:
        return f"{float(value):.1f}/s"
    except (TypeError, ValueError):
        return "-"


def _render_status(runtime_by_name: Dict[str, ServiceRuntime]) -> None:
    pipeline = _TELEMETRY.get("pipeline", {})
    semantic = _TELEMETRY.get("semantic_index", {})

    api_runtime = runtime_by_name.get("API")
    api_ok = False
    api_ms: Optional[float] = None
    if api_runtime and api_runtime.process.poll() is None:
        readiness = api_runtime.spec.get("readiness", {})
        if readiness.get("type") == "http":
            api_ok, api_ms = _probe_http_latency(str(readiness["url"]))

    live_state = str(pipeline.get("live_state") or "UNKNOWN")
    pipeline_state = str(pipeline.get("state") or "WAITING")
    queued = int(pipeline.get("queued") or 0)
    running = int(pipeline.get("running") or 0)
    lag = _format_age(pipeline.get("oldest_s"))
    done_rate = _format_rate(pipeline.get("done_per_s"))
    in_rate = _format_rate(pipeline.get("arrivals_per_s"))
    semantic_state = str(semantic.get("state") or "WAITING")
    vector_backlog = int(semantic.get("pending_vectors") or 0)
    vector_suffix = "+" if semantic.get("pending_vectors_capped") else ""
    index_rate = _format_rate(semantic.get("indexed_per_s"))
    api_text = f"{api_ms:.0f}ms" if api_ok and api_ms is not None else "DOWN"

    print(
        "[AIOS] "
        f"LIVE {live_state} | "
        f"pipeline {pipeline_state} {queued}q/{running}r lag {lag} "
        f"in {in_rate} out {done_rate} | "
        f"semantic {semantic_state} vectors {vector_backlog}{vector_suffix} "
        f"index {index_rate} | "
        f"API {api_text}",
        flush=True,
    )

    if pipeline_state in {"FALLING_BEHIND", "DEGRADED"}:
        lanes = list(pipeline.get("lanes") or [])
        lanes.sort(
            key=lambda row: (int(row.get("queued") or 0), float(row.get("oldest_s") or 0.0)),
            reverse=True,
        )
        for row in lanes[:4]:
            queued_lane = int(row.get("queued") or 0)
            running_lane = int(row.get("running") or 0)
            if queued_lane == 0 and running_lane == 0:
                continue
            print(
                "       "
                f"{row.get('resource')}/{row.get('lane')}: "
                f"{queued_lane}q/{running_lane}r "
                f"oldest {_format_age(row.get('oldest_s'))}",
                flush=True,
            )


def _print_failure_tail(runtime: ServiceRuntime) -> None:
    if not runtime.tail:
        return

    print(f"\nLast output from {runtime.name}:", flush=True)
    for line in runtime.tail:
        print(f"  [{runtime.name}] {line}", flush=True)


def _start_service(
    spec: dict,
    runtimes: List[ServiceRuntime],
    output_queue: "queue.Queue[tuple[str, str]]",
) -> ServiceRuntime:
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
    return runtime


def _wait_for_stage(
    stage_runtimes: List[ServiceRuntime],
    all_runtimes: List[ServiceRuntime],
    output_queue: "queue.Queue[tuple[str, str]]",
) -> None:
    runtime_by_name = {runtime.name: runtime for runtime in all_runtimes}

    while any(runtime.state == "STARTING" for runtime in stage_runtimes):
        _drain_output(output_queue, runtime_by_name)
        now = time.monotonic()

        for runtime in stage_runtimes:
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
    print(f"   Console mode: {LOG_MODE}")
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
        specs_by_name = {str(spec["name"]): spec for spec in SERVICES}

        for stage_name, stage_names in STARTUP_STAGES:
            print(f"\n▶ Starting {stage_name}...\n", flush=True)
            stage_runtimes = [
                _start_service(specs_by_name[name], runtimes, output_queue)
                for name in stage_names
            ]
            print(f"\nWaiting for {stage_name.lower()} readiness...\n", flush=True)
            _wait_for_stage(stage_runtimes, runtimes, output_queue)

        runtime_by_name = {runtime.name: runtime for runtime in runtimes}

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
        print(
            "   Terminal shows summarized health; set AIOS_LOG_MODE=verbose or "
            "debug for more detail."
        )
        print("   Press Ctrl+C to stop.\n", flush=True)

        reported_optional_exits = {
            runtime.name for runtime in optional if runtime.state == "DEGRADED"
        }
        next_status = time.monotonic()

        while True:
            _drain_output(output_queue, runtime_by_name)

            now = time.monotonic()
            if now >= next_status:
                _render_status(runtime_by_name)
                next_status = now + max(2.0, STATUS_INTERVAL)

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
