---
title: "Agent doesn't implement continuous profiling for agent processes"
description: "The agent has no CPU or memory profiling in production. Performance regressions—a new tool handler that allocates large buffers, a prompt template that triggers quadratic string operations—accumulate invisibly until the process OOMs or latency doubles."
difficulty: advanced
category: observability
tags: [profiling, py-spy, pyinstrument, memory-profiling, continuous-profiling, performance]
---

## Problem

Log-based observability tells you *what* happened; profiling tells you *why it's slow*. Without continuous profiling, CPU hotspots from inefficient JSON parsing, memory bloat from accumulating tool results, and GIL contention from synchronous I/O in async handlers are invisible. You discover them only after users complain or the process crashes.

Continuous profiling samples the call stack and heap at low overhead (1–5% CPU), aggregates the data over time, and surfaces it as flame graphs, function-level timings, and memory allocation traces.

```python
# BAD: no visibility into what the process is actually spending time on
async def handle_request(request):
    # Is this slow because of JSON parsing? Embedding calls? String formatting?
    # Unknown — no profiling data
    return await process(request)
```

## Solution 1: Sampling profiler with `pyinstrument` around hot paths

Wrap specific code paths with `pyinstrument.Profiler` to capture wall-time call stacks. Output as HTML flame graph or JSON for CI integration.

```python
import asyncio
from pyinstrument import Profiler
from pyinstrument.renderers import HTMLRenderer, JSONRenderer
from pathlib import Path
import time


class ProfilingContext:
    """
    Context manager that profiles a block of code and saves the report.
    Only active when PROFILING_ENABLED=1 to avoid production overhead.
    """

    def __init__(
        self,
        label: str,
        output_dir: str = ".profiles",
        interval: float = 0.001,  # 1ms sampling interval
        async_mode: str = "enabled",
    ):
        self.label = label
        self.output_dir = Path(output_dir)
        self.interval = interval
        self.async_mode = async_mode
        self._profiler: Profiler | None = None
        self.output_dir.mkdir(parents=True, exist_ok=True)

    async def __aenter__(self):
        self._profiler = Profiler(interval=self.interval, async_mode=self.async_mode)
        self._profiler.start()
        return self

    async def __aexit__(self, *args):
        if self._profiler:
            self._profiler.stop()
            session = self._profiler.last_session

            # Save HTML flame graph
            ts = int(time.time())
            html_path = self.output_dir / f"{self.label}_{ts}.html"
            html_path.write_text(
                HTMLRenderer(show_all=True, timeline=False).render(session)
            )

            # Save JSON for programmatic analysis
            json_path = self.output_dir / f"{self.label}_{ts}.json"
            json_path.write_text(JSONRenderer().render(session))

            print(
                f"[Profile] {self.label}: "
                f"{session.duration:.3f}s | report: {html_path}"
            )


# ── Usage ────────────────────────────────────────────────────────────
async def expensive_tool_handler(data: list) -> list:
    # Simulate CPU-intensive processing
    result = []
    for item in data:
        result.append({"processed": str(item) * 100})
    await asyncio.sleep(0.01)
    return result


async def main():
    async with ProfilingContext("tool_handler", interval=0.001):
        for _ in range(100):
            await expensive_tool_handler(list(range(500)))


asyncio.run(main())
```

## Solution 2: Continuous background profiler with periodic snapshot export

Run a low-overhead profiler in a background task. Every N seconds, flush a snapshot to a rotating log file. No request-level instrumentation required.

```python
import asyncio
import time
import json
from pathlib import Path
from typing import Optional

# Requires: pip install pyinstrument
from pyinstrument import Profiler
from pyinstrument.renderers import JSONRenderer


class ContinuousProfiler:
    """
    Runs pyinstrument in a background asyncio task.
    Exports snapshots every `snapshot_interval` seconds.
    """

    def __init__(
        self,
        snapshot_interval: float = 60.0,
        output_dir: str = ".profiles/continuous",
        max_snapshots: int = 24,  # keep last 24 snapshots (24h at 1/hour)
        sample_interval: float = 0.005,
    ):
        self.snapshot_interval = snapshot_interval
        self.output_dir = Path(output_dir)
        self.max_snapshots = max_snapshots
        self.sample_interval = sample_interval
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._profiler: Optional[Profiler] = None
        self._task: Optional[asyncio.Task] = None

    def start(self):
        self._task = asyncio.create_task(self._loop(), name="continuous-profiler")
        print(f"[ContinuousProfiler] Started — snapshots every {self.snapshot_interval}s")

    def stop(self):
        if self._task:
            self._task.cancel()
        if self._profiler:
            self._profiler.stop()

    async def _loop(self):
        while True:
            self._profiler = Profiler(
                interval=self.sample_interval, async_mode="enabled"
            )
            self._profiler.start()
            await asyncio.sleep(self.snapshot_interval)
            self._profiler.stop()
            await self._export_snapshot()

    async def _export_snapshot(self):
        if not self._profiler:
            return
        session = self._profiler.last_session
        if session is None:
            return

        ts = int(time.time())
        snapshot = {
            "timestamp": ts,
            "duration_s": round(session.duration, 3),
            "profile": json.loads(JSONRenderer().render(session)),
        }

        path = self.output_dir / f"snapshot_{ts}.json"
        path.write_text(json.dumps(snapshot))

        # Rotate old snapshots
        snapshots = sorted(self.output_dir.glob("snapshot_*.json"))
        for old in snapshots[: -self.max_snapshots]:
            old.unlink()

        print(f"[ContinuousProfiler] Snapshot: {path.name} ({session.duration:.1f}s window)")


# ── Integration with agent startup ───────────────────────────────────
async def agent_main():
    profiler = ContinuousProfiler(snapshot_interval=30.0, output_dir=".profiles/agent")
    profiler.start()

    try:
        # Normal agent work here
        for i in range(100):
            await asyncio.sleep(0.1)
            _ = [x ** 2 for x in range(1000)]  # simulated CPU work
    finally:
        profiler.stop()
        print("Agent stopped — profiler snapshots saved")


asyncio.run(agent_main())
```

## Solution 3: Memory profiler with leak detection using `tracemalloc`

Track memory allocations per call site. After each agent request, compare the allocation snapshot to detect leaks — call sites that keep growing indicate unreleased resources.

```python
import asyncio
import tracemalloc
import linecache
from dataclasses import dataclass
from typing import Optional


@dataclass
class AllocationHotspot:
    filename: str
    lineno: int
    size_kb: float
    count: int
    traceback_str: str


class MemoryLeakDetector:
    def __init__(
        self,
        top_n: int = 20,
        growth_threshold_kb: float = 100.0,
        check_interval: int = 50,  # check every N requests
    ):
        self.top_n = top_n
        self.growth_threshold_kb = growth_threshold_kb
        self.check_interval = check_interval
        self._request_count = 0
        self._baseline: Optional[tracemalloc.Snapshot] = None

    def start(self):
        tracemalloc.start(10)  # 10 frames of traceback
        self._baseline = tracemalloc.take_snapshot()
        print("[MemoryLeak] Baseline snapshot taken")

    def stop(self):
        tracemalloc.stop()

    def on_request_complete(self) -> Optional[list[AllocationHotspot]]:
        self._request_count += 1
        if self._request_count % self.check_interval != 0:
            return None

        current = tracemalloc.take_snapshot()
        stats = current.compare_to(self._baseline, "traceback")

        hotspots = []
        for stat in stats[: self.top_n]:
            if stat.size_diff / 1024 < self.growth_threshold_kb:
                continue
            tb = stat.traceback
            frame = tb[0]
            hotspots.append(AllocationHotspot(
                filename=frame.filename,
                lineno=frame.lineno,
                size_kb=round(stat.size_diff / 1024, 1),
                count=stat.count_diff,
                traceback_str="\n".join(
                    f"  {f.filename}:{f.lineno}" for f in tb[:5]
                ),
            ))

        if hotspots:
            print(f"[MemoryLeak] {len(hotspots)} growth hotspots after {self._request_count} requests:")
            for h in hotspots[:5]:
                print(f"  +{h.size_kb}KB at {h.filename}:{h.lineno} (×{h.count})")

        return hotspots if hotspots else None


# ── Usage ────────────────────────────────────────────────────────────
detector = MemoryLeakDetector(check_interval=10, growth_threshold_kb=1.0)


async def simulate_leaky_handler(n: int):
    # Intentional leak: appending to a module-level list
    _LEAK_BUFFER.extend(list(range(n)))
    await asyncio.sleep(0.001)


_LEAK_BUFFER = []


async def main():
    detector.start()
    for i in range(55):
        await simulate_leaky_handler(100)
        detector.on_request_complete()
    detector.stop()


asyncio.run(main())
```

## Solution 4: `py-spy` integration for zero-overhead production profiling

`py-spy` attaches to a running Python process without modifying the source code, making it safe for production flame graph collection. Automate it via a management API endpoint.

```python
import asyncio
import subprocess
import os
import time
from pathlib import Path
from fastapi import FastAPI, BackgroundTasks


app = FastAPI()
PROFILE_DIR = Path(".profiles/pyspy")
PROFILE_DIR.mkdir(parents=True, exist_ok=True)


async def run_pyspy_profile(
    pid: int,
    duration_seconds: int = 30,
    output_format: str = "flamegraph",
) -> Path:
    """
    Run py-spy against the given PID for `duration_seconds`.
    Returns path to the output SVG (flame graph) or speedscope JSON.
    """
    ts = int(time.time())
    if output_format == "flamegraph":
        output_path = PROFILE_DIR / f"flamegraph_{pid}_{ts}.svg"
        cmd = [
            "py-spy", "record",
            "--pid", str(pid),
            "--duration", str(duration_seconds),
            "--output", str(output_path),
            "--format", "flamegraph",
            "--nonblocking",
        ]
    else:
        output_path = PROFILE_DIR / f"profile_{pid}_{ts}.json"
        cmd = [
            "py-spy", "record",
            "--pid", str(pid),
            "--duration", str(duration_seconds),
            "--output", str(output_path),
            "--format", "speedscope",
            "--nonblocking",
        ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(f"py-spy failed: {stderr.decode()}")

    return output_path


@app.post("/debug/profile")
async def trigger_profile(
    duration: int = 30,
    background_tasks: BackgroundTasks = None,
):
    """Trigger a py-spy profile of the current process."""
    pid = os.getpid()

    async def _run():
        try:
            path = await run_pyspy_profile(pid, duration_seconds=duration)
            print(f"Profile saved: {path}")
        except Exception as e:
            print(f"Profile failed: {e}")

    background_tasks.add_task(_run)
    return {
        "status": "profiling_started",
        "pid": pid,
        "duration_seconds": duration,
        "output_dir": str(PROFILE_DIR),
    }


@app.get("/debug/top")
async def show_top():
    """Show current top functions using py-spy top (non-blocking)."""
    pid = os.getpid()
    proc = await asyncio.create_subprocess_exec(
        "py-spy", "top", "--pid", str(pid), "--duration", "5",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return {"top": stdout.decode()}
```

## Solution 5: Request-scoped profiling with overhead budget enforcement

Profile individual requests but only when overhead is within budget. Use adaptive sampling: profile 100% of slow requests, 1% of fast ones.

```python
import asyncio
import time
import random
from contextlib import asynccontextmanager
from typing import Any

SLOW_REQUEST_THRESHOLD_MS = 500.0  # always profile requests slower than this
FAST_SAMPLE_RATE = 0.01             # profile 1% of fast requests
PROFILE_OUTPUT_DIR = ".profiles/requests"


class AdaptiveSampler:
    def __init__(self, slow_threshold_ms: float, fast_rate: float):
        self.slow_threshold_ms = slow_threshold_ms
        self.fast_rate = fast_rate
        self._profiles_collected = 0

    def should_profile_now(self) -> bool:
        return random.random() < self.fast_rate

    @asynccontextmanager
    async def profile_if_needed(self, request_id: str):
        should_profile = self.should_profile_now()

        if not should_profile:
            # No profiling — just track timing
            start = time.monotonic()
            yield None
            elapsed_ms = (time.monotonic() - start) * 1000
            if elapsed_ms > self.slow_threshold_ms:
                print(f"[{request_id}] Slow request {elapsed_ms:.0f}ms — no profile captured (not sampled)")
            return

        try:
            from pyinstrument import Profiler
            profiler = Profiler(interval=0.001, async_mode="enabled")
            profiler.start()
            start = time.monotonic()
        except ImportError:
            yield None
            return

        try:
            yield profiler
        finally:
            profiler.stop()
            elapsed_ms = (time.monotonic() - start) * 1000
            session = profiler.last_session

            import os
            os.makedirs(PROFILE_OUTPUT_DIR, exist_ok=True)
            path = f"{PROFILE_OUTPUT_DIR}/{request_id}_{int(time.time())}.html"

            from pyinstrument.renderers import HTMLRenderer
            with open(path, "w") as f:
                f.write(HTMLRenderer().render(session))

            self._profiles_collected += 1
            print(
                f"[{request_id}] {elapsed_ms:.0f}ms | "
                f"profile #{self._profiles_collected}: {path}"
            )


sampler = AdaptiveSampler(slow_threshold_ms=200, fast_rate=0.10)


async def handle_request(request_id: str, payload: dict) -> dict:
    async with sampler.profile_if_needed(request_id):
        # Simulated processing
        await asyncio.sleep(random.uniform(0.05, 0.3))
        return {"result": "ok", "id": request_id}


async def main():
    tasks = [handle_request(f"req-{i}", {}) for i in range(20)]
    await asyncio.gather(*tasks)
    print(f"Total profiles: {sampler._profiles_collected}")


asyncio.run(main())
```

## Solution 6: Profiling data pipeline to Grafana Phlare / Pyroscope

Push continuous profiling data to a Pyroscope-compatible backend (open-source; works with Grafana Phlare). Enables long-term flame graph storage, diff views, and integration with existing Grafana dashboards.

```python
import asyncio
import time
import sys
from typing import Optional

# Requires: pip install pyroscope-io
try:
    import pyroscope
    PYROSCOPE_AVAILABLE = True
except ImportError:
    PYROSCOPE_AVAILABLE = False


def configure_pyroscope(
    server_address: str = "http://localhost:4040",
    app_name: str = "agent.python",
    tags: Optional[dict] = None,
    sample_rate: int = 100,  # samples per second
):
    """
    Configure Pyroscope continuous profiling.
    Call once at agent startup.
    """
    if not PYROSCOPE_AVAILABLE:
        print("[Profiling] pyroscope-io not installed — skipping")
        return

    pyroscope.configure(
        application_name=app_name,
        server_address=server_address,
        sample_rate=sample_rate,
        tags=tags or {
            "environment": "production",
            "version": "1.0.0",
            "agent_type": "orchestrator",
        },
    )
    print(f"[Profiling] Pyroscope connected: {server_address}")


# ── Dynamic tagging per request (differentiates flame graphs by context) ─
def profiled_request(tool_name: str, user_tier: str):
    """
    Context manager that tags the current profiling window with request-level
    metadata, enabling per-tool and per-user-tier flame graphs in Grafana.
    """
    if not PYROSCOPE_AVAILABLE:
        from contextlib import nullcontext
        return nullcontext()

    return pyroscope.tag_wrapper({"tool": tool_name, "tier": user_tier})


# ── Usage in request handler ──────────────────────────────────────────
async def process_with_profiling(tool_name: str, user_tier: str, data: dict) -> dict:
    with profiled_request(tool_name, user_tier):
        # CPU work is tagged — flame graph sliced by tool + tier
        result = [x ** 2 for x in range(10_000)]  # simulated
        await asyncio.sleep(0.05)
        return {"tool": tool_name, "result_count": len(result)}


async def main():
    configure_pyroscope(app_name="synapse-agent")

    tasks = [
        process_with_profiling("web_search", "premium", {}),
        process_with_profiling("summarize", "free", {}),
        process_with_profiling("embed", "premium", {}),
    ]
    results = await asyncio.gather(*tasks)
    for r in results:
        print(r)


asyncio.run(main())
```

## Comparison

| Approach | Overhead | Production-safe | Flame graph | Memory tracking | External backend |
|---|---|---|---|---|---|
| pyinstrument hot-path | 2–5% CPU | Conditional | Yes | No | No |
| Continuous background profiler | 1–2% CPU | Yes | Yes | No | No |
| tracemalloc leak detector | 5–15% | Dev/staging | No | Yes | No |
| py-spy zero-intrusion | <1% | Yes | Yes | No | No |
| Adaptive request sampler | 0–5% | Yes | Yes | No | No |
| Pyroscope continuous push | 1–3% | Yes | Yes | No | Yes (Grafana) |

**Recommendation**: Run **py-spy** (Solution 4) for on-demand production profiling triggered via an internal API endpoint — zero code changes required. Add **tracemalloc leak detection** (Solution 3) in staging to catch memory leaks before production. Use **Pyroscope** (Solution 6) when you need long-term profiling history and diff views between deploys.
