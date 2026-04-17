---
title: "Agent Doesn't Implement Agent Restart Reason Logging"
description: "Agents that restart without recording why they restarted make incident investigation guesswork: an OOM kill, a SIGTERM from orchestrator scale-in, an unhandled exception, and a scheduled rolling restart all look identical from the outside — the agent process just stops and starts. Implement restart reason logging that captures the termination signal, last error, memory usage at shutdown, active request count, and process uptime, then emits a structured startup event that distinguishes planned from unplanned restarts."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-agent-restart-reason-logging
tags: [restart-logging, process-lifecycle, crash-detection, termination-reason, startup-event, incident-investigation]
symptoms:
  - "Agent restarts show up as gaps in metrics but no record of why the restart occurred"
  - "Cannot distinguish an OOM kill from a SIGTERM from a clean deployment restart"
  - "Post-mortem investigations start with 'the agent restarted' but cannot determine the cause"
  - "No record of how many requests were in-flight when the agent shut down"
  - "Startup events do not include context from the previous process run"
---

## Why This Happens

Process restarts are external events from the agent's perspective — they are imposed by the operating system (OOM killer), the orchestrator (SIGTERM on scale-in), or the deployment system (rolling restart). By default, the agent's logging framework only records events while the process is running; the moment of termination and its cause are not captured because the logging code has no opportunity to run after a SIGKILL. Partial capture is possible: signal handlers can record SIGTERM reasons before shutdown; the exit code can be read by the new process from a file written before exit; and /proc/meminfo or runtime metrics can be snapshotted on shutdown. The new process should read these records and emit a startup event that contextualizes the restart.

## Solution 1: Shutdown Context Record

```python
import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from pathlib import Path


class TerminationReason(str, Enum):
    CLEAN_SHUTDOWN = "clean_shutdown"        # graceful SIGTERM handled
    SIGKILL = "sigkill"                      # likely OOM or force-kill
    UNHANDLED_EXCEPTION = "unhandled_exception"
    SIGTERM = "sigterm"                      # orchestrator-initiated
    SIGHUP = "sighup"                        # reload signal
    OOM_SUSPECTED = "oom_suspected"          # inferred from memory metrics
    UNKNOWN = "unknown"                      # new process, no record from previous


@dataclass
class ShutdownContextRecord:
    pid: int
    termination_reason: TerminationReason
    uptime_seconds: float
    active_requests_at_shutdown: int
    memory_rss_mb: float
    last_error: Optional[str]
    shutdown_at: float = field(default_factory=time.time)
    signal_received: Optional[str] = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "pid": self.pid,
            "termination_reason": self.termination_reason.value,
            "uptime_seconds": round(self.uptime_seconds, 1),
            "active_requests_at_shutdown": self.active_requests_at_shutdown,
            "memory_rss_mb": round(self.memory_rss_mb, 1),
            "last_error": self.last_error,
            "shutdown_at": self.shutdown_at,
            "signal_received": self.signal_received,
            **self.extra,
        }

    def save(self, path: str = "/tmp/agent_last_shutdown.json") -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, path: str = "/tmp/agent_last_shutdown.json") -> Optional["ShutdownContextRecord"]:
        try:
            data = json.loads(Path(path).read_text())
            return cls(
                pid=data["pid"],
                termination_reason=TerminationReason(data.get("termination_reason", "unknown")),
                uptime_seconds=data.get("uptime_seconds", 0),
                active_requests_at_shutdown=data.get("active_requests_at_shutdown", 0),
                memory_rss_mb=data.get("memory_rss_mb", 0),
                last_error=data.get("last_error"),
                shutdown_at=data.get("shutdown_at", 0),
                signal_received=data.get("signal_received"),
            )
        except Exception:
            return None
```

## Solution 2: Process Lifecycle Monitor

```python
import os
import signal
import time
from typing import Optional


class ProcessLifecycleMonitor:
    """
    Tracks process lifetime, active request count, and memory usage.
    Registers signal handlers to capture the termination reason at shutdown.
    """

    def __init__(self, context_path: str = "/tmp/agent_last_shutdown.json"):
        self._start_time = time.time()
        self._active_requests = 0
        self._last_error: Optional[str] = None
        self._context_path = context_path
        self._termination_reason = TerminationReason.UNKNOWN

    def register_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_sigterm)
        signal.signal(signal.SIGHUP, self._handle_sighup)
        # SIGKILL cannot be caught — its absence from a saved record
        # implies the process was killed without a signal handler running

    def _handle_sigterm(self, signum, frame) -> None:
        self._termination_reason = TerminationReason.SIGTERM
        self._save_shutdown_context(signal_received="SIGTERM")

    def _handle_sighup(self, signum, frame) -> None:
        self._termination_reason = TerminationReason.SIGHUP
        self._save_shutdown_context(signal_received="SIGHUP")

    def increment_active(self) -> None:
        self._active_requests += 1

    def decrement_active(self) -> None:
        self._active_requests = max(0, self._active_requests - 1)

    def record_error(self, error: str) -> None:
        self._last_error = error[:500]

    def _memory_rss_mb(self) -> float:
        try:
            import resource
            rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # macOS reports bytes, Linux reports kilobytes
            import sys
            if sys.platform == "darwin":
                return rss_kb / 1024 / 1024
            return rss_kb / 1024
        except Exception:
            return 0.0

    def _save_shutdown_context(
        self,
        reason: Optional[TerminationReason] = None,
        signal_received: Optional[str] = None,
    ) -> None:
        record = ShutdownContextRecord(
            pid=os.getpid(),
            termination_reason=reason or self._termination_reason,
            uptime_seconds=time.time() - self._start_time,
            active_requests_at_shutdown=self._active_requests,
            memory_rss_mb=self._memory_rss_mb(),
            last_error=self._last_error,
            signal_received=signal_received,
        )
        record.save(self._context_path)

    def clean_shutdown(self) -> None:
        self._save_shutdown_context(reason=TerminationReason.CLEAN_SHUTDOWN)

    def record_unhandled_exception(self, error: str) -> None:
        self._last_error = error[:500]
        self._save_shutdown_context(reason=TerminationReason.UNHANDLED_EXCEPTION)

    @property
    def uptime_seconds(self) -> float:
        return round(time.time() - self._start_time, 1)
```

## Solution 3: Startup Event Builder

```python
import os
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class StartupEvent:
    pid: int
    started_at: float
    environment: str
    previous_shutdown: Optional[ShutdownContextRecord]
    restart_classification: str      # "planned" | "unplanned" | "first_start"
    time_since_last_shutdown_seconds: Optional[float]
    memory_rss_mb: float = 0.0

    def to_dict(self) -> dict:
        prev = self.previous_shutdown
        return {
            "event": "agent_startup",
            "pid": self.pid,
            "started_at": self.started_at,
            "environment": self.environment,
            "restart_classification": self.restart_classification,
            "time_since_last_shutdown_s": self.time_since_last_shutdown_seconds,
            "previous_run": prev.to_dict() if prev else None,
            "memory_rss_mb": round(self.memory_rss_mb, 1),
        }


class StartupEventBuilder:
    """
    Reads the previous shutdown context on startup and classifies the restart.
    """

    PLANNED_REASONS = {
        TerminationReason.CLEAN_SHUTDOWN,
        TerminationReason.SIGTERM,
    }
    UNPLANNED_REASONS = {
        TerminationReason.UNHANDLED_EXCEPTION,
        TerminationReason.OOM_SUSPECTED,
        TerminationReason.SIGKILL,
    }

    def build(
        self,
        context_path: str = "/tmp/agent_last_shutdown.json",
        environment: str = "",
    ) -> StartupEvent:
        prev = ShutdownContextRecord.load(context_path)
        now = time.time()

        if prev is None:
            classification = "first_start"
            delta = None
        elif prev.termination_reason in self.PLANNED_REASONS:
            classification = "planned"
            delta = round(now - prev.shutdown_at, 1)
        elif prev.termination_reason in self.UNPLANNED_REASONS:
            classification = "unplanned"
            delta = round(now - prev.shutdown_at, 1)
        else:
            # UNKNOWN usually means previous process was SIGKILL'd (OOM)
            classification = "unplanned"
            delta = round(now - prev.shutdown_at, 1) if prev.shutdown_at else None

        return StartupEvent(
            pid=os.getpid(),
            started_at=now,
            environment=environment or os.getenv("ENVIRONMENT", "unknown"),
            previous_shutdown=prev,
            restart_classification=classification,
            time_since_last_shutdown_seconds=delta,
        )
```

## Solution 4: Restart Reason Logger

```python
import json
import time
from typing import Callable, List, Optional


class RestartReasonLogger:
    """
    Emits the startup event as a structured log record and accumulates
    restart history for trend analysis.
    """

    def __init__(
        self,
        write_fn: Optional[Callable[[dict], None]] = None,
        max_history: int = 100,
    ):
        self._write = write_fn or (lambda r: print(json.dumps(r)))
        self._history: List[dict] = []
        self._max = max_history

    def emit_startup(self, event: StartupEvent) -> None:
        record = event.to_dict()
        if len(self._history) >= self._max:
            self._history.pop(0)
        self._history.append(record)
        self._write(record)

    def unplanned_restart_rate(self, window_events: int = 20) -> dict:
        recent = self._history[-window_events:]
        if not recent:
            return {"events": 0}
        unplanned = [e for e in recent if e.get("restart_classification") == "unplanned"]
        return {
            "events": len(recent),
            "unplanned": len(unplanned),
            "unplanned_rate": round(len(unplanned) / len(recent), 4),
            "recent_reasons": [
                e.get("previous_run", {}).get("termination_reason", "unknown")
                for e in recent
                if e.get("previous_run")
            ][-10:],
        }
```

## Solution 5: OOM Inference Detector

```python
class OOMInferenceDetector:
    """
    Infers whether a previous process was OOM-killed based on available evidence:
    no shutdown record (SIGKILL leaves no time for signal handler), high memory
    at last checkpoint, and short time since shutdown.
    """

    def __init__(
        self,
        memory_threshold_mb: float = 1500.0,
        max_gap_seconds: float = 10.0,
    ):
        self._mem_threshold = memory_threshold_mb
        self._max_gap = max_gap_seconds

    def infer(self, prev: Optional[ShutdownContextRecord]) -> bool:
        if prev is None:
            return False   # no prior record — cannot infer
        if prev.termination_reason != TerminationReason.UNKNOWN:
            return False   # known reason — not an OOM inference case
        gap = __import__("time").time() - prev.shutdown_at
        high_memory = prev.memory_rss_mb >= self._mem_threshold
        short_gap = gap <= self._max_gap
        return high_memory and short_gap
```

## Solution 6: Restart Trend Dashboard

```python
import time


class RestartTrendDashboard:
    """
    Combines restart history, OOM inference, and lifecycle monitor state
    into a single operational view.
    """

    def __init__(
        self,
        logger: RestartReasonLogger,
        lifecycle: ProcessLifecycleMonitor,
        oom_detector: OOMInferenceDetector,
    ):
        self._logger = logger
        self._lifecycle = lifecycle
        self._oom = oom_detector

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "current_process": {
                "uptime_seconds": self._lifecycle.uptime_seconds,
                "active_requests": self._lifecycle._active_requests,
            },
            "restart_stats": self._logger.unplanned_restart_rate(20),
        }
```

## Comparison

| Approach | Signal Handling | Shutdown Persistence | OOM Inference | Startup Classification | Trend Analysis |
|---|---|---|---|---|---|
| ProcessLifecycleMonitor | Yes (SIGTERM/SIGHUP) | Yes (JSON file) | No | No | No |
| StartupEventBuilder | No | Via record | No | Yes (planned/unplanned) | No |
| RestartReasonLogger | No | No | No | Via event | Yes (rate) |
| OOMInferenceDetector | No | No | Yes (memory + gap) | No | No |
| RestartTrendDashboard | No | No | Via detector | No | Yes |

**Best for production**: Write the shutdown context file to a volume that survives container restarts — `/tmp` is typically ephemeral in containerized environments. Use a host-mounted path or a shared persistent volume. Register `ProcessLifecycleMonitor.register_signal_handlers()` as the very first action in `main()` — before any other initialization — so that a SIGTERM during startup is captured. Emit the startup event to your structured logging system immediately after `RestartReasonLogger.emit_startup()` so it lands in your log aggregator before any request is served; on-call engineers checking logs after an incident can then immediately see "unplanned restart, previous process had 2.1 GB RSS" as the first log line after the gap. Alert when `unplanned_rate` exceeds 0.25 over the last 20 restarts — three or more unplanned restarts in a short window is a crash loop.
