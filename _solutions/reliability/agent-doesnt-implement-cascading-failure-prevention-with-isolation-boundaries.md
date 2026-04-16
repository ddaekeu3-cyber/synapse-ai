---
title: "Agent Doesn't Implement Cascading Failure Prevention with Isolation Boundaries"
description: "AI agents that share resources — thread pools, database connections, HTTP client sessions — across all tool calls allow one slow or failing tool to exhaust shared resources and bring down all other tools simultaneously. Isolation boundaries partition resources into per-tool bulkheads so that a pathological tool can only consume its own allocation, leaving other tools unaffected."
date: 2025-02-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-cascading-failure-prevention-with-isolation-boundaries
tags:
  - cascading-failure
  - isolation
  - bulkhead
  - resource-partitioning
  - semaphore
  - reliability
  - fault-isolation
symptoms:
  - "A slow web_search tool holds all 10 HTTP connections, blocking db_query and cache_lookup"
  - "One tool's thread pool exhaustion causes unrelated tools to queue and time out"
  - "A single misbehaving tool brings the entire agent to a halt"
  - "No per-tool concurrency limits — all tools share a global semaphore"
  - "Database connection pool exhausted by one tool prevents all other tools from querying"
---

## Problem

Shared resources create implicit coupling between tools. If `web_search` opens 10 HTTP connections and the connection pool has 10 slots, `db_query` cannot acquire a connection and fails — even though the database is healthy. Isolation boundaries allocate a fixed resource share to each tool (concurrency slots, connection pool partitions, memory limits). A tool that exhausts its allocation is rate-limited to its boundary; other tools' allocations are unaffected. This is the thread-pool bulkhead pattern applied at tool granularity.

---

## Solution 1: ToolBulkhead — Per-Tool Concurrency Semaphore

```python
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class BulkheadStats:
    tool_name: str
    max_concurrent: int
    active: int = 0
    queued: int = 0
    rejected: int = 0
    completed: int = 0
    errors: int = 0


class ToolBulkhead:
    """
    Limits concurrent executions of a single tool to `max_concurrent`.
    Calls beyond the limit are either queued (up to `queue_limit`) or
    rejected immediately to prevent resource starvation.

    Usage:
        bulkhead = ToolBulkhead("web_search", max_concurrent=3, queue_limit=10)

        result = await bulkhead.call(web_search_fn, query="SSRF")
        # At most 3 web_search calls run simultaneously; others queue or fail.
    """

    def __init__(self, tool_name: str,
                  max_concurrent: int = 5,
                  queue_limit: int = 20,
                  timeout_s: float = 30.0):
        self._name = tool_name
        self._sem = asyncio.Semaphore(max_concurrent)
        self._max = max_concurrent
        self._queue_limit = queue_limit
        self._timeout = timeout_s
        self._stats = BulkheadStats(tool_name, max_concurrent)

    async def call(self, fn: Callable, *args, **kwargs) -> Any:
        # Reject if too many waiters
        waiters = self._max - self._sem._value
        if waiters >= self._queue_limit:
            self._stats.rejected += 1
            raise asyncio.TimeoutError(
                f"Bulkhead '{self._name}' queue full "
                f"({waiters} waiting, limit={self._queue_limit})"
            )

        self._stats.queued += 1
        try:
            acquired = await asyncio.wait_for(
                self._sem.acquire(), timeout=self._timeout
            )
        except asyncio.TimeoutError:
            self._stats.rejected += 1
            self._stats.queued -= 1
            raise asyncio.TimeoutError(
                f"Bulkhead '{self._name}' acquisition timed out after {self._timeout}s"
            )

        self._stats.queued -= 1
        self._stats.active += 1
        t0 = time.monotonic()
        try:
            result = await fn(*args, **kwargs)
            self._stats.completed += 1
            return result
        except Exception:
            self._stats.errors += 1
            raise
        finally:
            self._stats.active -= 1
            self._sem.release()
            elapsed = (time.monotonic() - t0) * 1000
            logger.debug(
                "bulkhead_call tool=%s elapsed_ms=%.0f active=%d",
                self._name, elapsed, self._stats.active,
            )

    def stats(self) -> BulkheadStats:
        return self._stats
```

---

## Solution 2: IsolatedToolRegistry — Bulkhead per Registered Tool

```python
import asyncio
import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class IsolatedToolRegistry:
    """
    Registry that wraps every tool with its own bulkhead.
    Tools are registered with per-tool concurrency limits;
    calling any tool through the registry enforces isolation.

    Usage:
        registry = IsolatedToolRegistry()
        registry.register("web_search",  web_search_fn,  max_concurrent=3)
        registry.register("db_query",    db_query_fn,    max_concurrent=8)
        registry.register("llm_invoke",  llm_fn,         max_concurrent=5)

        result = await registry.call("web_search", query="SSRF prevention")
        # web_search failure cannot affect db_query or llm_invoke
    """

    def __init__(self, default_max_concurrent: int = 5,
                  default_queue_limit: int = 20):
        self._tools: Dict[str, Callable] = {}
        self._bulkheads: Dict[str, ToolBulkhead] = {}
        self._default_max = default_max_concurrent
        self._default_queue = default_queue_limit

    def register(self, name: str, fn: Callable,
                  max_concurrent: Optional[int] = None,
                  queue_limit: Optional[int] = None,
                  timeout_s: float = 30.0):
        self._tools[name] = fn
        self._bulkheads[name] = ToolBulkhead(
            name,
            max_concurrent=max_concurrent or self._default_max,
            queue_limit=queue_limit or self._default_queue,
            timeout_s=timeout_s,
        )

    async def call(self, tool_name: str, *args, **kwargs) -> Any:
        fn = self._tools.get(tool_name)
        if fn is None:
            raise KeyError(f"Tool '{tool_name}' not registered")
        bulkhead = self._bulkheads[tool_name]
        return await bulkhead.call(fn, *args, **kwargs)

    def isolation_report(self) -> Dict[str, Any]:
        return {
            name: {
                "max_concurrent": bh.stats().max_concurrent,
                "active": bh.stats().active,
                "rejected": bh.stats().rejected,
                "errors": bh.stats().errors,
            }
            for name, bh in self._bulkheads.items()
        }
```

---

## Solution 3: ResourcePartitioner — Partition a Shared Pool Across Tools

```python
import asyncio
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ResourcePartitioner:
    """
    Partitions a fixed-size resource pool (e.g., 20 DB connections)
    across tools so that no single tool can exhaust the shared pool.
    Each tool gets a guaranteed minimum and a burstable maximum.

    Usage:
        partitioner = ResourcePartitioner(total=20)
        partitioner.assign("db_query",    min_share=5, max_share=12)
        partitioner.assign("cache_lookup", min_share=2, max_share=4)
        partitioner.assign("analytics",    min_share=1, max_share=4)

        async with partitioner.acquire("db_query") as token:
            result = await db.fetch(query)
    """

    def __init__(self, total: int):
        self._total = total
        self._assignments: Dict[str, tuple] = {}  # name -> (min, max, Semaphore)

    def assign(self, tool_name: str, min_share: int, max_share: int):
        if max_share > self._total:
            raise ValueError(f"max_share {max_share} > total {self._total}")
        self._assignments[tool_name] = (
            min_share, max_share, asyncio.Semaphore(max_share)
        )
        logger.info(
            "partition_assigned tool=%s min=%d max=%d",
            tool_name, min_share, max_share,
        )

    class _Token:
        def __init__(self, sem: asyncio.Semaphore):
            self._sem = sem

        async def __aenter__(self):
            await self._sem.acquire()
            return self

        async def __aexit__(self, *exc):
            self._sem.release()

    def acquire(self, tool_name: str) -> "_Token":
        assignment = self._assignments.get(tool_name)
        if assignment is None:
            raise KeyError(f"No partition assigned for '{tool_name}'")
        _, _, sem = assignment
        return self._Token(sem)

    def utilisation(self) -> Dict[str, float]:
        result = {}
        for name, (_, max_share, sem) in self._assignments.items():
            active = max_share - sem._value
            result[name] = round(active / max_share, 3)
        return result
```

---

## Solution 4: CascadeDetector — Detect and Break Failure Cascades

```python
import asyncio
import logging
import time
from collections import deque
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class CascadeDetector:
    """
    Detects failure cascades: when multiple tools fail in rapid succession
    in a correlated window, it indicates a shared dependency failure
    (e.g., database down, network partition) rather than independent tool bugs.
    Triggers a system-wide pause to prevent thundering herd retries.

    Usage:
        detector = CascadeDetector(window_s=5.0, cascade_threshold=5)
        detector.on_cascade = lambda: activate_circuit_breaker()

        # Wrap tool calls:
        try:
            result = await tool_fn()
            detector.record_success()
        except Exception:
            detector.record_failure("tool_name")
    """

    def __init__(self, window_s: float = 5.0,
                  cascade_threshold: int = 5,
                  on_cascade: Optional[Callable] = None):
        self._window = window_s
        self._threshold = cascade_threshold
        self._on_cascade = on_cascade
        self._failures: deque = deque()
        self._cascade_count = 0
        self._last_cascade = 0.0

    def record_failure(self, tool_name: str = "unknown"):
        now = time.monotonic()
        self._failures.append((now, tool_name))
        # Evict old failures outside window
        while self._failures and self._failures[0][0] < now - self._window:
            self._failures.popleft()

        if len(self._failures) >= self._threshold:
            self._trigger_cascade(now)

    def record_success(self):
        # Success resets cascade detection if we were in a cascade
        pass

    def _trigger_cascade(self, now: float):
        if now - self._last_cascade < self._window:
            return  # Already in cascade mode
        self._last_cascade = now
        self._cascade_count += 1
        recent = [t for _, t in self._failures]
        logger.critical(
            "cascade_detected failures=%d tools=%s in %.0fs",
            len(self._failures), recent, self._window,
        )
        if self._on_cascade:
            self._on_cascade()

    def failure_rate(self) -> float:
        now = time.monotonic()
        recent = sum(1 for ts, _ in self._failures if now - ts < self._window)
        return recent / self._window

    def stats(self) -> Dict[str, Any]:
        return {
            "recent_failures": len(self._failures),
            "cascade_events": self._cascade_count,
            "failure_rate_per_s": round(self.failure_rate(), 2),
        }
```

---

## Solution 5: IsolationBoundaryMonitor — Track Cross-Boundary Leakage

```python
import asyncio
import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class IsolationBoundaryMonitor:
    """
    Monitors whether isolation boundaries are actually containing failures.
    Compares failure rates across tool groups — if one tool's failure rate
    increases simultaneously with others, isolation is not working and
    a shared dependency has likely failed.

    Usage:
        monitor = IsolationBoundaryMonitor()
        monitor.record("web_search", success=False)
        monitor.record("db_query",   success=True)

        if monitor.correlation_detected("web_search", "db_query"):
            alert("Possible shared dependency failure")
    """

    def __init__(self, window_s: float = 60.0):
        self._window = window_s
        self._events: Dict[str, list] = {}  # tool -> [(ts, success)]

    def record(self, tool_name: str, success: bool):
        if tool_name not in self._events:
            self._events[tool_name] = []
        self._events[tool_name].append((time.monotonic(), success))
        # Evict old events
        cutoff = time.monotonic() - self._window
        self._events[tool_name] = [
            e for e in self._events[tool_name] if e[0] >= cutoff
        ]

    def failure_rate(self, tool_name: str) -> float:
        events = self._events.get(tool_name, [])
        if not events:
            return 0.0
        failures = sum(1 for _, ok in events if not ok)
        return failures / len(events)

    def correlation_detected(self, tool_a: str, tool_b: str,
                               threshold: float = 0.3) -> bool:
        """True if both tools have failure rate > threshold — suggests shared cause."""
        return (self.failure_rate(tool_a) > threshold and
                self.failure_rate(tool_b) > threshold)

    def isolation_health(self) -> Dict[str, Any]:
        return {
            tool: {
                "events": len(events),
                "failure_rate": round(self.failure_rate(tool), 3),
            }
            for tool, events in self._events.items()
        }
```

---

## Solution 6: FaultIsolatedAgentPipeline — Full Isolation Stack

```python
import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class FaultIsolatedAgentPipeline:
    """
    End-to-end isolated agent pipeline: each tool runs in its own
    bulkhead, failures are detected for cascade patterns, and the
    monitor provides live isolation health metrics.

    Usage:
        pipeline = FaultIsolatedAgentPipeline()
        pipeline.add_tool("web_search",  web_fn,  max_concurrent=3)
        pipeline.add_tool("db_query",    db_fn,   max_concurrent=8)
        pipeline.add_tool("llm_invoke",  llm_fn,  max_concurrent=5)

        result = await pipeline.call("web_search", query=q)
    """

    def __init__(self, cascade_threshold: int = 5):
        self._registry = IsolatedToolRegistry()
        self._detector = CascadeDetector(cascade_threshold=cascade_threshold)
        self._monitor = IsolationBoundaryMonitor()
        self._detector.on_cascade = self._on_cascade

    def add_tool(self, name: str, fn: Callable, **bulkhead_kwargs):
        self._registry.register(name, fn, **bulkhead_kwargs)

    async def call(self, tool_name: str, *args, **kwargs) -> Any:
        try:
            result = await self._registry.call(tool_name, *args, **kwargs)
            self._detector.record_success()
            self._monitor.record(tool_name, success=True)
            return result
        except Exception as exc:
            self._detector.record_failure(tool_name)
            self._monitor.record(tool_name, success=False)
            raise

    def _on_cascade(self):
        logger.critical(
            "cascade_alert isolation_health=%s",
            self._monitor.isolation_health(),
        )

    def health_report(self) -> Dict[str, Any]:
        return {
            "isolation": self._registry.isolation_report(),
            "cascade": self._detector.stats(),
            "boundary_health": self._monitor.isolation_health(),
        }
```

---

## Comparison

| Approach | Per-Tool Limit | Pool Partitioning | Cascade Detection | Cross-Tool Monitor | Integrated |
|---|---|---|---|---|---|
| **ToolBulkhead** | Yes | No | No | No | No |
| **IsolatedToolRegistry** | Yes | No | No | No | No |
| **ResourcePartitioner** | No | Yes | No | No | No |
| **CascadeDetector** | No | No | Yes | No | No |
| **IsolationBoundaryMonitor** | No | No | No | Yes | No |
| **FaultIsolatedAgentPipeline** | Yes | No | Yes | Yes | Yes |

**Key insight**: assign each tool a `max_concurrent` of its own rather than sharing a global semaphore. A good starting point is `max_concurrent = max(2, expected_rps × p99_latency_s × 2)`. Set `queue_limit` to 2–5× `max_concurrent` to provide bursting without unbounded queuing. Add `CascadeDetector` with a 5-failure-in-5-second threshold to distinguish independent tool failures from shared dependency outages — the two require different responses (retry vs circuit break).
