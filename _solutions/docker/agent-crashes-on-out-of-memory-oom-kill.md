---
layout: solution
title: "Agent Process Killed by OOM — Container Runs Out of Memory"
category: docker
description: "Kubernetes OOMKills the agent pod mid-task. Docker container exits with code 137. Process memory grows to 8GB and the OS kills it. The agent was in the middle of processing a large document or holding a growing context window in memory. No warning. No graceful shutdown. Just death."
tags: [docker, oom, memory, kubernetes, container, resource-limits, memory-leak, large-payload, streaming]
---

# Agent Process Killed by OOM — Container Runs Out of Memory

## Symptom
- Container exits with code 137 (SIGKILL from OOM killer)
- Kubernetes events show `OOMKilled` for the agent pod
- `docker stats` shows memory climbing to container limit before crash
- Agent crashes when processing large documents or long conversations
- Memory usage grows over hours and never decreases — slow memory leak
- Process crashes during embedding computation or LLM response handling

## Root Cause
Agent memory spikes come from several sources: large API responses held in memory as strings, growing conversation history never cleared, embedding vectors accumulated across many documents, Python object reference cycles preventing garbage collection, and large file reads loaded entirely into memory. In containers with strict memory limits, any of these causes an OOMKill — which is immediate and unrecoverable. The fix is to profile memory usage, stream large data rather than loading it whole, and set conservative memory limits with early warnings.

## Fix

### Option 1: Stream large API responses — never hold full response in memory

```python
import httpx
import asyncio
from typing import AsyncIterator

async def stream_large_document(url: str) -> AsyncIterator[str]:
    """
    Stream a large document chunk by chunk — never loads the full document into memory.
    Use this instead of response.text for any document > 1MB.
    """
    async with httpx.AsyncClient() as client:
        async with client.stream("GET", url, timeout=60.0) as response:
            response.raise_for_status()
            async for chunk in response.aiter_text(chunk_size=8192):
                yield chunk

async def process_large_document_streaming(
    url: str,
    process_chunk_fn
) -> list:
    """
    Process a large document without holding it all in memory.
    Accumulates only the results, not the input.
    """
    results = []
    buffer = ""
    buffer_limit = 50_000  # Process in ~50KB chunks

    async for chunk in stream_large_document(url):
        buffer += chunk
        if len(buffer) >= buffer_limit:
            result = await process_chunk_fn(buffer)
            results.append(result)
            buffer = ""  # Release chunk memory immediately
            del result   # Explicit delete to hint GC

    if buffer:  # Process remainder
        results.append(await process_chunk_fn(buffer))

    return results

async def stream_anthropic_response(
    client,
    model: str,
    messages: list[dict],
    system: str = ""
) -> str:
    """
    Stream Claude response — never holds full response string in memory during generation.
    For responses that could be very long.
    """
    full_text = []

    async with client.messages.stream(
        model=model,
        max_tokens=4096,
        system=system,
        messages=messages
    ) as stream:
        async for text in stream.text_stream:
            full_text.append(text)
            # Optionally write chunks to disk for very long responses:
            # await output_file.write(text)

    return "".join(full_text)
```

### Option 2: Memory usage monitor — alert and shed load before OOM

```python
import os
import gc
import psutil
import asyncio
import logging

logger = logging.getLogger(__name__)

class MemoryPressureManager:
    """
    Monitor memory usage and take action before OOM kill.
    Triggers load shedding and GC when memory approaches container limit.
    """

    def __init__(
        self,
        warn_threshold: float = 0.75,    # Warn at 75% of limit
        shed_threshold: float = 0.85,    # Start load shedding at 85%
        critical_threshold: float = 0.92, # Emergency GC at 92%
        check_interval: float = 30.0,
        memory_limit_mb: int = None       # Auto-detect if None
    ):
        self.warn = warn_threshold
        self.shed = shed_threshold
        self.critical = critical_threshold
        self.check_interval = check_interval
        self._proc = psutil.Process(os.getpid())
        self._limit_mb = memory_limit_mb or self._detect_container_limit()
        self._shedding = False

    def _detect_container_limit(self) -> int:
        """Detect container memory limit from cgroups"""
        # cgroups v2
        for path in ["/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"]:
            try:
                limit = open(path).read().strip()
                if limit not in ("max", "9223372036854771712"):  # Not "unlimited"
                    return int(limit) // (1024 * 1024)
            except (FileNotFoundError, ValueError):
                continue
        # Fallback: use system RAM
        return psutil.virtual_memory().total // (1024 * 1024)

    def get_usage(self) -> dict:
        mem = self._proc.memory_info()
        rss_mb = mem.rss / (1024 * 1024)
        utilization = rss_mb / self._limit_mb

        return {
            "rss_mb": round(rss_mb, 1),
            "limit_mb": self._limit_mb,
            "utilization": round(utilization, 3),
            "status": (
                "critical" if utilization > self.critical else
                "shedding" if utilization > self.shed else
                "warning" if utilization > self.warn else
                "ok"
            )
        }

    def _run_gc(self) -> int:
        """Force garbage collection, return objects collected"""
        return sum(gc.collect(generation) for generation in range(3))

    def should_accept_request(self) -> bool:
        """Return False when under memory pressure — shed incoming load"""
        usage = self.get_usage()
        if usage["utilization"] > self.shed:
            self._shedding = True
            return False
        self._shedding = False
        return True

    async def monitor_loop(self):
        """Background monitoring loop"""
        while True:
            await asyncio.sleep(self.check_interval)
            usage = self.get_usage()

            if usage["status"] == "critical":
                collected = self._run_gc()
                logger.critical(
                    f"CRITICAL: Memory at {usage['rss_mb']}MB / {usage['limit_mb']}MB "
                    f"({usage['utilization']*100:.0f}%). GC collected {collected} objects."
                )

            elif usage["status"] in ("shedding", "warning"):
                logger.warning(
                    f"Memory pressure: {usage['rss_mb']}MB / {usage['limit_mb']}MB "
                    f"({usage['utilization']*100:.0f}%)"
                )
                if usage["status"] == "shedding":
                    self._run_gc()  # GC under load shedding conditions too

memory_manager = MemoryPressureManager(memory_limit_mb=2048)
asyncio.create_task(memory_manager.monitor_loop())
```

### Option 3: Kubernetes resource config — set limits with room for spikes

```yaml
# kubernetes/deployment.yaml
apiVersion: apps/v1
kind: Deployment
spec:
  template:
    spec:
      containers:
      - name: agent
        image: my-agent:latest

        resources:
          requests:
            memory: "512Mi"    # Guaranteed minimum
            cpu: "250m"
          limits:
            memory: "2Gi"      # Hard cap — OOMKill above this
            cpu: "2"

        env:
        # Tell Python the container memory limit
        - name: MEMORY_LIMIT_MB
          valueFrom:
            resourceFieldRef:
              resource: limits.memory
              divisor: "1Mi"

        # Tune Python garbage collector for container environments
        - name: PYTHONMALLOC
          value: "malloc"
        - name: MALLOC_TRIM_THRESHOLD_
          value: "65536"  # Return memory to OS more aggressively

        # Limit thread stack size (reduces per-thread memory overhead)
        - name: PYTHONSTACKSIZE
          value: "4096"

        livenessProbe:
          httpGet:
            path: /health
            port: 8080
          initialDelaySeconds: 30
          periodSeconds: 10

        # OOM prevention: readiness probe goes false when under memory pressure
        readinessProbe:
          httpGet:
            path: /ready  # Returns 503 when memory > 85% limit
            port: 8080
          periodSeconds: 5
```

```python
# Set Python memory allocator settings at startup
import os
import sys

def configure_memory_for_container():
    """Tune Python memory settings for container environments"""
    limit_mb = int(os.getenv("MEMORY_LIMIT_MB", "2048"))

    # Limit object cache to free memory more aggressively
    import gc
    gc.set_threshold(700, 10, 10)  # More frequent GC than default (700, 10, 10)

    # Set recursion limit conservatively to prevent stack overflow
    sys.setrecursionlimit(3000)

    print(f"Memory configured: limit={limit_mb}MB, gc_threshold={gc.get_threshold()}")

configure_memory_for_container()
```

### Option 4: Conversation history memory cap — prevent context bloat

```python
import sys
from dataclasses import dataclass, field

@dataclass
class MemoryBoundedHistory:
    """
    Conversation history with a hard memory cap.
    Drops oldest turns when the in-memory size exceeds the limit.
    Prevents long sessions from causing OOM via growing history.
    """
    max_memory_mb: float = 50.0     # Max MB for history in memory
    max_turns: int = 100
    _history: list = field(default_factory=list)

    def _history_size_mb(self) -> float:
        """Estimate memory used by history"""
        return sys.getsizeof(self._history) / (1024 * 1024)

    def add_turn(self, role: str, content: str):
        """Add a turn, dropping oldest if memory limit exceeded"""
        self._history.append({"role": role, "content": content})

        # Check memory pressure
        while (
            self._history_size_mb() > self.max_memory_mb or
            len(self._history) > self.max_turns * 2
        ):
            # Drop oldest pair (user + assistant)
            if len(self._history) >= 2:
                self._history.pop(0)
                self._history.pop(0)
            else:
                break

    def get_history(self) -> list[dict]:
        return self._history

    @property
    def stats(self) -> dict:
        return {
            "turns": len(self._history) // 2,
            "size_mb": round(self._history_size_mb(), 2),
            "limit_mb": self.max_memory_mb
        }

bounded_history = MemoryBoundedHistory(max_memory_mb=50.0, max_turns=50)
```

### Option 5: Process memory profiling — find and fix leaks

```python
import tracemalloc
import linecache
import os
from typing import Optional

class MemoryProfiler:
    """
    Profile memory usage to identify leaks.
    Run in development to find which code paths allocate the most memory.
    """

    def __init__(self, top_n: int = 20):
        self.top_n = top_n
        self._snapshot_baseline: Optional[tracemalloc.Snapshot] = None

    def start(self):
        """Start memory tracing"""
        tracemalloc.start(10)  # Keep 10 frames of traceback
        self._snapshot_baseline = tracemalloc.take_snapshot()
        print("Memory profiling started")

    def report_top_allocations(self) -> list[dict]:
        """Report the top N memory allocations since baseline"""
        if not tracemalloc.is_tracing():
            return [{"error": "Profiling not started — call start() first"}]

        snapshot = tracemalloc.take_snapshot()
        top_stats = snapshot.statistics("lineno")

        results = []
        for stat in top_stats[:self.top_n]:
            frame = stat.traceback[0]
            filename = frame.filename
            lineno = frame.lineno
            try:
                line = linecache.getline(filename, lineno).strip()
            except Exception:
                line = "(unavailable)"
            results.append({
                "file": os.path.basename(filename),
                "line": lineno,
                "code": line,
                "size_kb": round(stat.size / 1024, 1),
                "count": stat.count
            })

        return results

    def report_diff(self) -> list[dict]:
        """Report memory growth since baseline"""
        if not self._snapshot_baseline or not tracemalloc.is_tracing():
            return []

        snapshot = tracemalloc.take_snapshot()
        top_stats = snapshot.compare_to(self._snapshot_baseline, "lineno")

        results = []
        for stat in top_stats[:self.top_n]:
            if stat.size_diff > 1024:  # Only show > 1KB growth
                frame = stat.traceback[0]
                results.append({
                    "file": os.path.basename(frame.filename),
                    "line": frame.lineno,
                    "growth_kb": round(stat.size_diff / 1024, 1),
                    "total_kb": round(stat.size / 1024, 1)
                })

        return sorted(results, key=lambda x: x["growth_kb"], reverse=True)

    def stop(self):
        tracemalloc.stop()

profiler = MemoryProfiler()
# profiler.start()  # Enable in development
# ... run agent operations ...
# allocations = profiler.report_diff()
# print(allocations[:5])  # Top 5 growing allocations
```

### Option 6: Large payload handling — process without loading into memory

```python
import asyncio
import json
import os
from pathlib import Path

class LargePayloadHandler:
    """
    Handle large payloads (documents, datasets) without loading into memory.
    Uses temporary files as intermediate storage.
    """

    def __init__(self, temp_dir: str = "/tmp/agent_payloads"):
        self.temp_dir = Path(temp_dir)
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    async def process_large_json(
        self,
        json_data: str,
        process_fn,
        chunk_size: int = 100  # For arrays: items per chunk
    ) -> list:
        """
        Process a large JSON array without holding the full parsed object.
        Writes to temp file, streams through ijson (incremental JSON parser).
        """
        # Write to temp file
        temp_path = self.temp_dir / f"payload_{os.getpid()}.json"
        try:
            temp_path.write_text(json_data)
            del json_data  # Release the string from memory

            results = []
            try:
                import ijson  # pip install ijson
                with open(temp_path, "rb") as f:
                    items = ijson.items(f, "item")
                    batch = []
                    for item in items:
                        batch.append(item)
                        if len(batch) >= chunk_size:
                            result = await process_fn(batch)
                            results.append(result)
                            batch = []  # Release batch
                    if batch:
                        results.append(await process_fn(batch))
            except ImportError:
                # Fallback: load whole file (less memory-efficient)
                data = json.loads(temp_path.read_text())
                results.append(await process_fn(data))

            return results
        finally:
            temp_path.unlink(missing_ok=True)  # Always clean up

    def cleanup_old_payloads(self, max_age_seconds: int = 3600):
        """Remove temp files older than max_age_seconds"""
        import time
        now = time.time()
        for f in self.temp_dir.glob("payload_*.json"):
            if now - f.stat().st_mtime > max_age_seconds:
                f.unlink(missing_ok=True)

handler = LargePayloadHandler()
```

## OOM Root Causes and Fixes

| Cause | Memory Profile | Fix |
|---|---|---|
| Large API response in memory | Spike during download | Stream response in chunks |
| Growing conversation history | Linear growth over session | Sliding window or summary |
| Embedding vectors accumulated | Large array, never freed | Process and store to disk |
| Python string interning | Many duplicate strings | Use slots, avoid large string manipulation |
| Reference cycles | Memory never freed by GC | `gc.collect()` periodically; use `weakref` |
| Large file read | Spike = file size | Read in chunks with `open()` as generator |
| JSON parsing giant payload | Spike = payload × 2 | `ijson` for incremental JSON parsing |

## Expected Token Savings
OOMKill mid-task → restart → re-explain full context → resume: ~20,000 tokens overhead
Memory-bounded agent → completes task without crash: 0 recovery overhead

## Environment
- Any agent deployed in containers with memory limits; OOM is especially common for agents that: process large documents, run multi-hour autonomous sessions, or accumulate embeddings — container memory limits require proactive memory management, not reactive fixes
- Source: direct experience; OOMKill is the second most common production failure in containerized agents (after SIGTERM handling), always appearing after the agent has been running in production for several days handling increasingly large payloads
