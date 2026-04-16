---
title: "Agent Doesn't Implement Tool Call Success Rate SLO Tracking"
description: "Agents that measure tool call outcomes without SLO targets have no automated way to detect when reliability degrades below acceptable thresholds: a tool whose success rate drops from 99% to 91% may not trigger any alert because no target was defined. Implement SLO tracking for tool call success rates that defines error budgets, burns down budget as failures occur, and alerts when burn rate threatens the monthly target."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-tool-call-success-rate-slo-tracking
tags: [slo, error-budget, success-rate, tool-reliability, burn-rate, sre-practices]
symptoms:
  - "No defined success rate target for any tool — all tools are treated equally regardless of criticality"
  - "Tool failure rate spikes go undetected until users complain"
  - "On-call engineers have no quantitative threshold for when to page vs. monitor"
  - "Error budget is never tracked — teams do not know how much reliability margin remains"
  - "Post-incident reviews cannot determine whether the SLO was violated during an incident"
---

## Why This Happens

Success rate without a target is a metric without meaning — you can observe it but cannot act on it. SLO tracking requires three things: a target (e.g., 99.5% success over 30 days), an error budget derived from that target (0.5% of calls may fail), and a burn rate that alerts when failures are consuming the budget faster than the window allows. Most agent observability implementations log success and failure counts but never compute the derived burn-rate signal that enables proactive alerting before the SLO window closes.

## Solution 1: Tool SLO Definition

```python
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class ToolSLO:
    """Defines a success rate SLO for a single tool."""
    tool_name: str
    target_success_rate: float          # e.g. 0.995 for 99.5%
    window_seconds: int = 2592000       # 30 days default
    fast_burn_window_seconds: int = 3600    # 1-hour window for fast burn detection
    slow_burn_window_seconds: int = 21600   # 6-hour window for slow burn detection
    fast_burn_threshold: float = 14.4   # >14.4× budget consumption rate = page now
    slow_burn_threshold: float = 6.0    # >6× budget consumption rate = ticket
    min_requests_for_slo: int = 10      # ignore SLO until N requests observed

    @property
    def error_budget_fraction(self) -> float:
        return 1.0 - self.target_success_rate

    def budget_consumed_fraction(self, error_rate: float) -> float:
        """How much of the budget is consumed by an observed error rate."""
        if self.error_budget_fraction == 0:
            return float("inf") if error_rate > 0 else 0.0
        return error_rate / self.error_budget_fraction


@dataclass
class SLORegistry:
    slos: dict = field(default_factory=dict)   # tool_name -> ToolSLO

    def register(self, slo: ToolSLO) -> None:
        self.slos[slo.tool_name] = slo

    def get(self, tool_name: str) -> Optional[ToolSLO]:
        return self.slos.get(tool_name)

    def all(self) -> List[ToolSLO]:
        return list(self.slos.values())
```

## Solution 2: Rolling Success Rate Window

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Tuple


class RollingSuccessRateWindow:
    """
    Tracks success/failure events in a sliding time window.
    Computes success rate efficiently over arbitrary sub-windows.
    """

    def __init__(self, window_seconds: int = 2592000, max_events: int = 1_000_000):
        self._window = window_seconds
        self._max = max_events
        self._events: Deque[Tuple[float, bool]] = deque()   # (timestamp, success)
        self._lock = Lock()

    def record(self, success: bool) -> None:
        now = time.time()
        with self._lock:
            self._events.append((now, success))
            if len(self._events) > self._max:
                self._events.popleft()

    def _evict(self, cutoff: float) -> None:
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def rate(self, sub_window_seconds: Optional[int] = None) -> Tuple[float, int]:
        """Returns (success_rate, total_requests) over the sub-window."""
        now = time.time()
        window = sub_window_seconds or self._window
        cutoff = now - window
        with self._lock:
            relevant = [(ts, ok) for ts, ok in self._events if ts >= cutoff]
        if not relevant:
            return 1.0, 0
        total = len(relevant)
        successes = sum(1 for _, ok in relevant if ok)
        return successes / total, total

    def error_rate(self, sub_window_seconds: Optional[int] = None) -> Tuple[float, int]:
        rate, total = self.rate(sub_window_seconds)
        return 1.0 - rate, total
```

## Solution 3: Error Budget Tracker

```python
import time
from typing import Optional


class ErrorBudgetTracker:
    """
    Computes remaining error budget and burn rate for a tool SLO.
    Burn rate > threshold triggers an alert condition.
    """

    def __init__(self, slo: ToolSLO, window: RollingSuccessRateWindow):
        self._slo = slo
        self._window = window

    def remaining_budget_fraction(self) -> float:
        """Fraction of error budget not yet consumed in the full SLO window."""
        error_rate, total = self._window.error_rate(self._slo.window_seconds)
        if total < self._slo.min_requests_for_slo:
            return 1.0
        consumed = self._slo.budget_consumed_fraction(error_rate)
        return max(0.0, 1.0 - consumed)

    def burn_rate(self, sub_window_seconds: int) -> float:
        """
        Burn rate = (error rate in sub-window) / (error budget fraction).
        A burn rate of 1.0 means budget is being consumed at exactly the SLO rate.
        A burn rate of 14.4 means the budget will be exhausted in ~1/14.4 of the window.
        """
        error_rate, total = self._window.error_rate(sub_window_seconds)
        if total < self._slo.min_requests_for_slo:
            return 0.0
        return self._slo.budget_consumed_fraction(error_rate)

    def alert_status(self) -> dict:
        fast_burn = self.burn_rate(self._slo.fast_burn_window_seconds)
        slow_burn = self.burn_rate(self._slo.slow_burn_window_seconds)
        remaining = self.remaining_budget_fraction()

        if fast_burn >= self._slo.fast_burn_threshold:
            severity = "page"
        elif slow_burn >= self._slo.slow_burn_threshold:
            severity = "ticket"
        elif remaining < 0.10:
            severity = "warning"   # less than 10% budget left
        else:
            severity = "ok"

        return {
            "tool_name": self._slo.tool_name,
            "severity": severity,
            "fast_burn_rate": round(fast_burn, 3),
            "slow_burn_rate": round(slow_burn, 3),
            "remaining_budget_pct": round(remaining * 100, 2),
            "target_success_rate": self._slo.target_success_rate,
        }
```

## Solution 4: SLO-Instrumented Tool Wrapper

```python
import time
from typing import Any, Callable, Dict, Optional


class SLOInstrumentedToolWrapper:
    """
    Wraps tool calls to record outcomes against SLO windows.
    Attaches SLO alert status to each call result for downstream visibility.
    """

    def __init__(
        self,
        slo_registry: SLORegistry,
        windows: Dict[str, RollingSuccessRateWindow],
        trackers: Dict[str, ErrorBudgetTracker],
    ):
        self._registry = slo_registry
        self._windows = windows
        self._trackers = trackers

    def _ensure_tool(self, tool_name: str) -> None:
        if tool_name not in self._windows:
            slo = self._registry.get(tool_name)
            if slo is None:
                slo = ToolSLO(tool_name=tool_name, target_success_rate=0.99)
            window = RollingSuccessRateWindow(slo.window_seconds)
            self._windows[tool_name] = window
            self._trackers[tool_name] = ErrorBudgetTracker(slo, window)

    async def call(
        self,
        tool_name: str,
        fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> dict:
        self._ensure_tool(tool_name)
        start = time.time()
        success = True
        error_msg = None
        result = None
        try:
            result = await fn(*args, **kwargs)
        except Exception as exc:
            success = False
            error_msg = str(exc)
            raise
        finally:
            self._windows[tool_name].record(success)
            latency_ms = round((time.time() - start) * 1000, 2)

        alert = self._trackers[tool_name].alert_status()
        return {
            "result": result,
            "tool_name": tool_name,
            "success": success,
            "latency_ms": latency_ms,
            "slo_alert": alert,
        }
```

## Solution 5: Multi-Tool SLO Status Aggregator

```python
import time
from typing import Dict, List


class MultiToolSLOAggregator:
    """
    Polls all tracked tools and returns a ranked list of SLO health,
    sorted by severity so the worst-performing tools appear first.
    """

    SEVERITY_RANK = {"page": 0, "ticket": 1, "warning": 2, "ok": 3}

    def __init__(self, trackers: Dict[str, ErrorBudgetTracker]):
        self._trackers = trackers

    def status_all(self) -> List[dict]:
        statuses = [tracker.alert_status() for tracker in self._trackers.values()]
        return sorted(statuses, key=lambda s: self.SEVERITY_RANK.get(s["severity"], 99))

    def slo_compliance_summary(self) -> dict:
        statuses = self.status_all()
        return {
            "generated_at": time.time(),
            "total_tools": len(statuses),
            "page_count": sum(1 for s in statuses if s["severity"] == "page"),
            "ticket_count": sum(1 for s in statuses if s["severity"] == "ticket"),
            "warning_count": sum(1 for s in statuses if s["severity"] == "warning"),
            "ok_count": sum(1 for s in statuses if s["severity"] == "ok"),
            "tools_below_target": [
                s["tool_name"] for s in statuses if s["severity"] != "ok"
            ],
        }
```

## Solution 6: SLO Burn Rate Alert Log

```python
import time
from typing import List


class SLOBurnRateAlertLog:
    """
    Records SLO alert state transitions and provides a history of
    when tools entered and exited alert states for post-incident review.
    """

    def __init__(self, max_records: int = 10000):
        self._records: List[dict] = []
        self._max = max_records
        self._last_severity: dict = {}

    def observe(self, alert_status: dict) -> bool:
        """
        Records an alert status entry. Returns True if severity changed.
        """
        tool = alert_status["tool_name"]
        severity = alert_status["severity"]
        prev = self._last_severity.get(tool, "ok")
        changed = severity != prev
        self._last_severity[tool] = severity

        if changed or severity != "ok":
            if len(self._records) >= self._max:
                self._records.pop(0)
            self._records.append({
                "ts": time.time(),
                "tool_name": tool,
                "severity": severity,
                "previous_severity": prev,
                "transition": changed,
                "fast_burn_rate": alert_status.get("fast_burn_rate"),
                "remaining_budget_pct": alert_status.get("remaining_budget_pct"),
            })

        return changed

    def recent_alerts(self, window_seconds: float = 3600.0) -> List[dict]:
        cutoff = time.time() - window_seconds
        return [r for r in self._records if r["ts"] >= cutoff and r["severity"] != "ok"]

    def summary(self, window_seconds: float = 86400.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        transitions = [r for r in recent if r["transition"]]
        return {
            "window_seconds": window_seconds,
            "total_alert_entries": len(recent),
            "state_transitions": len(transitions),
            "tools_that_paged": list({r["tool_name"] for r in recent if r["severity"] == "page"}),
            "tools_that_ticketed": list({r["tool_name"] for r in recent if r["severity"] == "ticket"}),
        }
```

## Comparison

| Approach | SLO Definition | Error Budget | Burn Rate | Multi-Tool | Alert History |
|---|---|---|---|---|---|
| ToolSLO + SLORegistry | Yes | Yes (derived) | No | Via registry | No |
| RollingSuccessRateWindow | No | No | No | No | No |
| ErrorBudgetTracker | Via SLO | Yes | Yes (fast+slow) | No | No |
| SLOInstrumentedToolWrapper | Via registry | Via tracker | Via tracker | Yes | No |
| MultiToolSLOAggregator | No | No | No | Yes | No |
| SLOBurnRateAlertLog | No | No | No | Yes | Yes |

**Best for production**: Use the dual burn-rate model (fast window + slow window) from Google SRE practices — a single burn-rate threshold has poor recall for slow degradations and poor precision for short spikes. Set `fast_burn_threshold=14.4` (consumes 2% of a 30-day budget in 1 hour) and `slow_burn_threshold=6.0` (consumes 5% of budget in 6 hours). Register explicit SLOs for every external-dependency tool; leave internal tools at the default 99% target. Use `SLOBurnRateAlertLog.summary()` in post-incident reviews: the state transition log shows exactly when burn rate exceeded thresholds relative to when the incident was detected.
