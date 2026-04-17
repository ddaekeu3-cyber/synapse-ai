---
title: "Agent Doesn't Implement Tool Error Rate Tracking Per Tool"
description: "Agents that log tool failures without aggregating them per-tool cannot distinguish a systematically broken tool from transient noise: a web search tool returning 503s on every call looks identical in general error logs to a one-off timeout. Implement per-tool error rate tracking with sliding windows, error categorization, and automatic alerting when any tool's error rate exceeds its configured threshold."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-tool-error-rate-tracking-per-tool
tags: [tool-error-rate, per-tool-metrics, error-categorization, sliding-window, tool-reliability, alert-threshold]
symptoms:
  - "One tool failing 90% of the time not visible in overall agent error rate metrics"
  - "No way to tell whether tool failures are timeouts, auth errors, or schema mismatches"
  - "Tool error spikes require manual log grep to attribute to a specific tool"
  - "No per-tool SLO — all tools share a global error rate threshold"
  - "Transient errors and permanent failures counted identically in dashboards"
---

## Why This Happens

Agent observability usually measures end-to-end request success, not per-tool reliability. A single tool that fails on every call is masked when averaged with nine tools that succeed, producing an acceptable aggregate error rate while silently delivering broken behavior. Per-tool tracking requires counting calls and failures per tool name, categorizing failure types (timeout, auth, schema, upstream), computing rolling error rates, and comparing them against per-tool thresholds that reflect the tool's expected reliability characteristics.

## Solution 1: Tool Call Outcome

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
import time


class ToolErrorCategory(str, Enum):
    TIMEOUT = "timeout"
    AUTH = "auth"               # 401/403 from upstream
    RATE_LIMIT = "rate_limit"   # 429
    UPSTREAM_ERROR = "upstream_error"  # 5xx from upstream
    SCHEMA_MISMATCH = "schema_mismatch"  # result failed schema validation
    INVALID_ARGS = "invalid_args"       # bad arguments passed to tool
    NOT_FOUND = "not_found"     # 404 / resource not found
    UNKNOWN = "unknown"


@dataclass
class ToolCallOutcome:
    tool_name: str
    success: bool
    latency_ms: float
    error_category: Optional[ToolErrorCategory] = None
    error_message: str = ""
    request_id: str = ""
    session_id: str = ""
    recorded_at: float = field(default_factory=time.time)

    @classmethod
    def from_exception(
        cls,
        tool_name: str,
        exc: Exception,
        latency_ms: float,
        **kwargs,
    ) -> "ToolCallOutcome":
        msg = str(exc).lower()
        if "timeout" in msg or "timed out" in msg:
            cat = ToolErrorCategory.TIMEOUT
        elif "401" in msg or "403" in msg or "unauthorized" in msg or "forbidden" in msg:
            cat = ToolErrorCategory.AUTH
        elif "429" in msg or "rate limit" in msg:
            cat = ToolErrorCategory.RATE_LIMIT
        elif any(c in msg for c in ("500", "502", "503", "504")):
            cat = ToolErrorCategory.UPSTREAM_ERROR
        elif "404" in msg or "not found" in msg:
            cat = ToolErrorCategory.NOT_FOUND
        else:
            cat = ToolErrorCategory.UNKNOWN
        return cls(
            tool_name=tool_name,
            success=False,
            latency_ms=latency_ms,
            error_category=cat,
            error_message=str(exc)[:300],
            **kwargs,
        )
```

## Solution 2: Per-Tool Error Rate Window

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Optional, Tuple


class PerToolErrorRateWindow:
    """
    Sliding window error rate tracker for a single tool.
    Tracks success/failure timestamps and computes error rate over the window.
    """

    def __init__(self, window_seconds: float = 300.0):
        self._window = window_seconds
        self._outcomes: Deque[Tuple[float, bool]] = deque()  # (ts, success)
        self._lock = Lock()
        self._total_calls = 0
        self._total_errors = 0

    def record(self, success: bool) -> None:
        now = time.time()
        with self._lock:
            self._outcomes.append((now, success))
            self._total_calls += 1
            if not success:
                self._total_errors += 1
            # Evict expired entries
            cutoff = now - self._window
            while self._outcomes and self._outcomes[0][0] < cutoff:
                self._outcomes.popleft()

    def error_rate(self) -> float:
        now = time.time()
        cutoff = now - self._window
        with self._lock:
            recent = [(ts, ok) for ts, ok in self._outcomes if ts >= cutoff]
        if not recent:
            return 0.0
        errors = sum(1 for _, ok in recent if not ok)
        return round(errors / len(recent), 6)

    def call_count(self, window: Optional[float] = None) -> int:
        w = window or self._window
        cutoff = time.time() - w
        with self._lock:
            return sum(1 for ts, _ in self._outcomes if ts >= cutoff)

    def stats(self) -> dict:
        rate = self.error_rate()
        return {
            "error_rate": rate,
            "window_calls": self.call_count(),
            "lifetime_calls": self._total_calls,
            "lifetime_errors": self._total_errors,
        }
```

## Solution 3: Tool Error Rate Registry

```python
from typing import Dict, List, Optional


class ToolErrorRateRegistry:
    """
    Maintains per-tool error rate windows and categorized error counters.
    Provides aggregate views across all registered tools.
    """

    def __init__(self, window_seconds: float = 300.0):
        self._window = window_seconds
        self._windows: Dict[str, PerToolErrorRateWindow] = {}
        self._category_counts: Dict[str, Dict[str, int]] = {}

    def _ensure(self, tool_name: str) -> None:
        if tool_name not in self._windows:
            self._windows[tool_name] = PerToolErrorRateWindow(self._window)
            self._category_counts[tool_name] = {}

    def record(self, outcome: ToolCallOutcome) -> None:
        self._ensure(outcome.tool_name)
        self._windows[outcome.tool_name].record(outcome.success)
        if not outcome.success and outcome.error_category:
            cat = outcome.error_category.value
            counts = self._category_counts[outcome.tool_name]
            counts[cat] = counts.get(cat, 0) + 1

    def error_rate(self, tool_name: str) -> float:
        w = self._windows.get(tool_name)
        return w.error_rate() if w else 0.0

    def all_rates(self) -> Dict[str, float]:
        return {name: w.error_rate() for name, w in self._windows.items()}

    def worst_tools(self, top_n: int = 5) -> List[dict]:
        rates = self.all_rates()
        sorted_tools = sorted(rates.items(), key=lambda x: -x[1])
        result = []
        for tool_name, rate in sorted_tools[:top_n]:
            w = self._windows[tool_name]
            result.append({
                "tool_name": tool_name,
                "error_rate": rate,
                "window_calls": w.call_count(),
                "top_error_categories": sorted(
                    self._category_counts.get(tool_name, {}).items(),
                    key=lambda x: -x[1],
                )[:3],
            })
        return result
```

## Solution 4: Per-Tool SLO Policy

```python
from dataclasses import dataclass, field
from typing import Dict


@dataclass
class ToolSLOPolicy:
    tool_name: str
    max_error_rate: float = 0.05          # 5% error rate threshold
    min_calls_to_evaluate: int = 10       # need at least N calls before alerting
    window_seconds: float = 300.0


DEFAULT_TOOL_SLO_POLICIES: Dict[str, ToolSLOPolicy] = {
    "web_search": ToolSLOPolicy("web_search", max_error_rate=0.10),
    "code_execution": ToolSLOPolicy("code_execution", max_error_rate=0.02),
    "database_query": ToolSLOPolicy("database_query", max_error_rate=0.01),
    "file_read": ToolSLOPolicy("file_read", max_error_rate=0.01),
    "http_request": ToolSLOPolicy("http_request", max_error_rate=0.08),
}
```

## Solution 5: Tool Error Rate Alert Manager

```python
import time
from typing import Callable, Dict, List, Optional, Set


class ToolErrorRateAlertManager:
    """
    Compares per-tool error rates against SLO policies and fires
    alert callbacks when a tool exceeds its threshold.
    Uses cooldown periods to avoid alert spam.
    """

    def __init__(
        self,
        registry: ToolErrorRateRegistry,
        policies: Dict[str, ToolSLOPolicy],
        alert_fn: Callable[[dict], None],
        alert_cooldown_s: float = 300.0,
        default_policy: Optional[ToolSLOPolicy] = None,
    ):
        self._registry = registry
        self._policies = policies
        self._alert_fn = alert_fn
        self._cooldown = alert_cooldown_s
        self._default = default_policy or ToolSLOPolicy("*", max_error_rate=0.10)
        self._last_alert: Dict[str, float] = {}

    def check_all(self) -> List[dict]:
        fired = []
        for tool_name, window in self._registry._windows.items():
            policy = self._policies.get(tool_name, self._default)
            if window.call_count() < policy.min_calls_to_evaluate:
                continue

            rate = window.error_rate()
            if rate <= policy.max_error_rate:
                continue

            last = self._last_alert.get(tool_name, 0.0)
            if time.time() - last < self._cooldown:
                continue

            self._last_alert[tool_name] = time.time()
            alert = {
                "event": "tool_error_rate_exceeded",
                "tool_name": tool_name,
                "error_rate": rate,
                "threshold": policy.max_error_rate,
                "window_calls": window.call_count(),
                "ts": time.time(),
            }
            try:
                self._alert_fn(alert)
            except Exception:
                pass
            fired.append(alert)
        return fired
```

## Solution 6: Tool Error Rate Dashboard

```python
import time


class ToolErrorRateDashboard:
    """
    Combines per-tool error rates, category breakdowns, SLO status,
    and worst-offender ranking into a single operational view.
    """

    def __init__(
        self,
        registry: ToolErrorRateRegistry,
        alert_manager: ToolErrorRateAlertManager,
        policies: Dict[str, ToolSLOPolicy],
    ):
        self._registry = registry
        self._alert_manager = alert_manager
        self._policies = policies

    def render(self) -> dict:
        all_rates = self._registry.all_rates()
        slo_status = {}
        for tool_name, rate in all_rates.items():
            policy = self._policies.get(tool_name)
            if policy:
                w = self._registry._windows[tool_name]
                slo_status[tool_name] = {
                    "error_rate": rate,
                    "threshold": policy.max_error_rate,
                    "slo_met": rate <= policy.max_error_rate,
                    "window_calls": w.call_count(),
                    "lifetime_calls": w._total_calls,
                    "top_error_categories": sorted(
                        self._registry._category_counts.get(tool_name, {}).items(),
                        key=lambda x: -x[1],
                    )[:3],
                }

        return {
            "generated_at": time.time(),
            "tool_count": len(all_rates),
            "slo_status": slo_status,
            "worst_tools": self._registry.worst_tools(top_n=5),
            "tools_breaching_slo": [
                name for name, s in slo_status.items() if not s["slo_met"]
            ],
        }
```

## Comparison

| Approach | Per-Tool Window | Error Categorization | SLO Policy | Alert Firing | Worst-Tool Ranking |
|---|---|---|---|---|---|
| PerToolErrorRateWindow | Yes (sliding) | No | No | No | No |
| ToolErrorRateRegistry | Yes (per tool) | Yes (lifetime counts) | No | No | Yes |
| ToolErrorRateAlertManager | Via registry | No | Yes | Yes (cooldown) | No |
| ToolErrorRateDashboard | Via registry | Via registry | Via policies | Via manager | Yes |

**Best for production**: Set `min_calls_to_evaluate=20` before alerting — error rates on fewer than 20 calls have high variance and produce noisy alerts. Use separate `ToolSLOPolicy` thresholds per tool: a web search tool with an external dependency warrants a 10% threshold while an internal database query tool should be held to 1%. Enable `ToolErrorCategory.AUTH` alerts with zero cooldown — an auth error spike means a credential has expired and needs immediate attention regardless of error rate. Monitor `worst_tools()` daily: consistent appearance of the same tool at the top is a reliability investment signal, not just an operational metric.
