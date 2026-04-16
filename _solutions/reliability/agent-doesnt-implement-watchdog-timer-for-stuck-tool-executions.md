---
title: "Agent Doesn't Implement Watchdog Timer for Stuck Tool Executions"
description: "Agents relying on asyncio.wait_for or a single timeout per tool call are vulnerable to tools that hang indefinitely when the underlying I/O never fires a timeout: a database query blocked on a lock, an HTTP connection that accepted but never sent data, or a subprocess that stalled without triggering a timeout exception. Implement a watchdog timer that monitors running tool calls from an external thread, forcibly cancels operations that exceed a wall-clock deadline, and reports the stall with diagnostic context."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-watchdog-timer-for-stuck-tool-executions
tags: [watchdog-timer, stuck-tool, deadlock-detection, forced-cancellation, wall-clock-timeout, liveness-monitoring]
symptoms:
  - "Tool call hangs indefinitely — asyncio.wait_for timeout never fires because the event loop is blocked"
  - "A database query waiting on a lock holds the agent session open for 20 minutes"
  - "No external mechanism to detect that a tool has been running for 3× its expected duration"
  - "Subprocess tool stalls without raising an exception — the agent waits forever"
  - "On-call engineer sees agent CPU at 0% and memory unchanged — classic stuck state"
---

## Why This Happens

`asyncio.wait_for` cancels a coroutine but cannot interrupt synchronous code inside it. If a tool calls a blocking library function (a database driver, a subprocess, a file read on a network mount), the event loop may be blocked and `wait_for` never triggers. Even pure-async code can stall if the upstream socket accepts the connection but the remote server never sends data. A watchdog timer runs in a dedicated OS thread, measures wall-clock time independently of the event loop, and forcibly cancels or kills the stuck operation from outside.

## Solution 1: Watchdog Registration Record

```python
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Optional


class WatchdogStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


@dataclass
class WatchdogRecord:
    watch_id: str
    tool_name: str
    deadline: float             # absolute wall-clock time (time.time())
    started_at: float
    status: WatchdogStatus = WatchdogStatus.RUNNING
    cancel_fn: Optional[Callable] = None   # called on expiry
    completed_at: Optional[float] = None
    expired_at: Optional[float] = None
    session_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def seconds_remaining(self) -> float:
        return max(0.0, self.deadline - time.time())

    def elapsed_seconds(self) -> float:
        end = self.completed_at or self.expired_at or time.time()
        return round(end - self.started_at, 3)

    def is_expired(self) -> bool:
        return time.time() >= self.deadline and self.status == WatchdogStatus.RUNNING
```

## Solution 2: Watchdog Timer Thread

```python
import threading
import time
from typing import Dict, List


class WatchdogTimerThread:
    """
    Dedicated daemon thread that polls registered watches every `poll_interval_seconds`
    and calls the cancel function on any watch that has exceeded its deadline.
    Runs independently of the asyncio event loop.
    """

    def __init__(self, poll_interval_seconds: float = 0.5):
        self._interval = poll_interval_seconds
        self._watches: Dict[str, WatchdogRecord] = {}
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True, name="watchdog-timer")
        self._running = False

    def start(self) -> None:
        self._running = True
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def register(self, record: WatchdogRecord) -> None:
        with self._lock:
            self._watches[record.watch_id] = record

    def complete(self, watch_id: str) -> None:
        with self._lock:
            record = self._watches.get(watch_id)
            if record and record.status == WatchdogStatus.RUNNING:
                record.status = WatchdogStatus.COMPLETED
                record.completed_at = time.time()

    def cancel(self, watch_id: str) -> None:
        with self._lock:
            record = self._watches.get(watch_id)
            if record:
                record.status = WatchdogStatus.CANCELLED

    def active_watches(self) -> List[WatchdogRecord]:
        with self._lock:
            return [r for r in self._watches.values() if r.status == WatchdogStatus.RUNNING]

    def _run(self) -> None:
        while self._running:
            now = time.time()
            with self._lock:
                expired = [
                    r for r in self._watches.values()
                    if r.status == WatchdogStatus.RUNNING and now >= r.deadline
                ]
            for record in expired:
                record.status = WatchdogStatus.EXPIRED
                record.expired_at = time.time()
                if record.cancel_fn:
                    try:
                        record.cancel_fn()
                    except Exception:
                        pass
            time.sleep(self._interval)

    def summary(self) -> dict:
        with self._lock:
            records = list(self._watches.values())
        by_status = {}
        for r in records:
            by_status[r.status] = by_status.get(r.status, 0) + 1
        return {
            "total_registered": len(records),
            "by_status": by_status,
            "active": sum(1 for r in records if r.status == WatchdogStatus.RUNNING),
        }
```

## Solution 3: Watchdog-Protected Tool Executor

```python
import asyncio
import threading
import time
import uuid
from typing import Any, Callable, Optional


class WatchdogProtectedToolExecutor:
    """
    Executes tool calls under a watchdog: if the call exceeds
    deadline_seconds, the watchdog fires cancel_fn and raises TimeoutError.
    Works even when asyncio.wait_for cannot cancel blocked sync code.
    """

    def __init__(self, watchdog: WatchdogTimerThread):
        self._watchdog = watchdog
        self._expired_count = 0
        self._total_count = 0

    async def call(
        self,
        tool_name: str,
        tool_fn: Callable,
        deadline_seconds: float = 30.0,
        session_id: str = "",
        **kwargs: Any,
    ) -> Any:
        self._total_count += 1
        watch_id = uuid.uuid4().hex[:12]
        task: Optional[asyncio.Task] = None
        expired_event = threading.Event()

        def cancel_fn():
            expired_event.set()
            if task and not task.done():
                task.cancel()

        record = WatchdogRecord(
            watch_id=watch_id,
            tool_name=tool_name,
            deadline=time.time() + deadline_seconds,
            started_at=time.time(),
            cancel_fn=cancel_fn,
            session_id=session_id,
        )
        self._watchdog.register(record)

        try:
            task = asyncio.create_task(tool_fn(**kwargs))
            result = await asyncio.wait_for(
                asyncio.shield(task),
                timeout=deadline_seconds + 1.0,  # outer safety net
            )
            self._watchdog.complete(watch_id)
            return result

        except asyncio.CancelledError:
            if expired_event.is_set():
                self._expired_count += 1
                raise TimeoutError(
                    f"Tool '{tool_name}' watchdog expired after {deadline_seconds}s "
                    f"(watch_id={watch_id})"
                )
            raise

        except asyncio.TimeoutError:
            self._expired_count += 1
            self._watchdog.complete(watch_id)
            raise TimeoutError(f"Tool '{tool_name}' timed out after {deadline_seconds}s")

        finally:
            if task and not task.done():
                task.cancel()

    def stats(self) -> dict:
        return {
            "total_calls": self._total_count,
            "watchdog_expirations": self._expired_count,
            "expiration_rate": round(
                self._expired_count / max(self._total_count, 1), 4
            ),
        }
```

## Solution 4: Per-Tool Deadline Registry

```python
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class ToolDeadlinePolicy:
    tool_name: str
    deadline_seconds: float
    warn_at_seconds: Optional[float] = None   # fire warning before expiry
    expected_p99_seconds: Optional[float] = None  # for anomaly detection


class PerToolDeadlineRegistry:
    """
    Stores deadline policies per tool name.
    Unregistered tools get the default deadline.
    """

    def __init__(self, default_deadline_seconds: float = 30.0):
        self._default = default_deadline_seconds
        self._policies: Dict[str, ToolDeadlinePolicy] = {}

    def register(self, policy: ToolDeadlinePolicy) -> None:
        self._policies[policy.tool_name] = policy

    def get_deadline(self, tool_name: str) -> float:
        policy = self._policies.get(tool_name)
        return policy.deadline_seconds if policy else self._default

    def get_policy(self, tool_name: str) -> ToolDeadlinePolicy:
        return self._policies.get(
            tool_name,
            ToolDeadlinePolicy(
                tool_name=tool_name,
                deadline_seconds=self._default,
            ),
        )
```

## Solution 5: Watchdog Stall Reporter

```python
import time
from typing import List


class WatchdogStallReporter:
    """
    Detects tools that are approaching their deadline (within warn_at_seconds)
    and fires a pre-expiry alert. Also summarizes stall patterns for post-incident review.
    """

    def __init__(
        self,
        watchdog: WatchdogTimerThread,
        deadline_registry: PerToolDeadlineRegistry,
    ):
        self._watchdog = watchdog
        self._registry = deadline_registry
        self._stall_log: List[dict] = []

    def check_approaching_deadlines(self) -> List[dict]:
        warnings = []
        for record in self._watchdog.active_watches():
            policy = self._registry.get_policy(record.tool_name)
            if policy.warn_at_seconds is None:
                continue
            remaining = record.seconds_remaining()
            if remaining <= policy.warn_at_seconds:
                warning = {
                    "watch_id": record.watch_id,
                    "tool_name": record.tool_name,
                    "session_id": record.session_id,
                    "seconds_remaining": round(remaining, 1),
                    "elapsed_seconds": round(record.elapsed_seconds(), 1),
                    "deadline_seconds": policy.deadline_seconds,
                }
                warnings.append(warning)
                self._stall_log.append({"ts": time.time(), "type": "approaching", **warning})
        return warnings

    def report_expired(self, record: WatchdogRecord) -> None:
        self._stall_log.append({
            "ts": time.time(),
            "type": "expired",
            "watch_id": record.watch_id,
            "tool_name": record.tool_name,
            "elapsed_seconds": record.elapsed_seconds(),
            "session_id": record.session_id,
        })

    def stall_summary(self) -> dict:
        by_tool: dict = {}
        for entry in self._stall_log:
            tool = entry.get("tool_name", "unknown")
            by_tool[tool] = by_tool.get(tool, 0) + 1
        return {
            "total_stall_events": len(self._stall_log),
            "by_tool": dict(sorted(by_tool.items(), key=lambda x: -x[1])),
        }
```

## Solution 6: Watchdog Dashboard

```python
import time


class WatchdogDashboard:
    """Combines watchdog thread summary, stall report, and executor stats."""

    def __init__(
        self,
        watchdog: WatchdogTimerThread,
        executor: WatchdogProtectedToolExecutor,
        reporter: WatchdogStallReporter,
    ):
        self._watchdog = watchdog
        self._executor = executor
        self._reporter = reporter

    def render(self) -> dict:
        approaching = self._reporter.check_approaching_deadlines()
        executor_stats = self._executor.stats()
        alerts = []
        if approaching:
            alerts.append({
                "type": "approaching_deadline",
                "count": len(approaching),
                "tools": [w["tool_name"] for w in approaching],
            })
        if executor_stats["expiration_rate"] > 0.02:
            alerts.append({
                "type": "high_expiration_rate",
                "rate": executor_stats["expiration_rate"],
                "message": "More than 2% of tool calls are expiring under watchdog.",
            })
        return {
            "generated_at": time.time(),
            "watchdog_thread": self._watchdog.summary(),
            "executor_stats": executor_stats,
            "stall_summary": self._reporter.stall_summary(),
            "approaching_deadline_warnings": approaching,
            "alerts": alerts,
            "healthy": len(alerts) == 0,
        }
```

## Comparison

| Approach | External Thread | Async Cancel | Pre-Expiry Warning | Per-Tool Deadline | Stall Reporting |
|---|---|---|---|---|---|
| WatchdogTimerThread | Yes (daemon) | Via cancel_fn | No | No | No |
| WatchdogProtectedToolExecutor | Via thread | Yes | No | No | No |
| PerToolDeadlineRegistry | No | No | Partial | Yes | No |
| WatchdogStallReporter | No | No | Yes | Via registry | Yes |
| WatchdogDashboard | No | No | No | No | Yes |

**Best for production**: Start `WatchdogTimerThread` at agent startup and keep it running for the process lifetime — it is a daemon thread so it won't prevent clean shutdown. Set per-tool deadlines in `PerToolDeadlineRegistry` based on observed P99 latency with a 3× safety margin: a tool whose P99 is 2s should have a 6s deadline. Set `warn_at_seconds` to half the deadline so you get a warning before expiry rather than only learning about stalls after the damage is done. Monitor `expiration_rate` in the dashboard — a rate above 1% indicates either the deadlines are too tight or a dependency is degrading.
