---
title: "Agent Doesn't Implement Cost Per Session Type Breakdown"
description: "Agents that track total cost without breaking it down by session type cannot answer 'which workflow costs 10× more than the others?' A customer-support session and a code-generation session may use the same model but have wildly different token economics. Implement cost attribution that tags every LLM call with a session type, aggregates cost per type over time, and alerts when a session type's cost-per-session exceeds its baseline."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-cost-per-session-type-breakdown
tags: [cost-attribution, session-type, cost-breakdown, llm-cost, unit-economics, cost-anomaly]
symptoms:
  - "Total monthly cost is known but no breakdown by feature, workflow, or session type"
  - "A new session type was added and doubled costs but it takes weeks to identify it"
  - "Cannot compute cost-per-session for any specific workflow to inform pricing decisions"
  - "All LLM calls are logged to the same cost bucket regardless of what triggered them"
  - "Cost anomalies are detected only when the monthly bill arrives"
---

## Why This Happens

Cost attribution requires tagging every LLM call with context about what triggered it. Without this tagging, all calls aggregate into a single "LLM cost" line. Adding session type tagging requires a small instrumentation change — passing a type identifier alongside each call — but yields disproportionate insight: it enables unit economics (cost per support ticket, cost per code review), anomaly detection per workflow, and informed decisions about where optimization efforts should focus.

## Solution 1: Session Type and Cost Record

```python
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class SessionTypeCostRecord:
    record_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    session_type: str = ""           # e.g. "customer_support", "code_review", "rag_query"
    session_id: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    operation: str = "chat"          # "chat" | "embed" | "rerank"
    recorded_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_usage(
        cls,
        session_type: str,
        session_id: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost_per_1k_prompt: float,
        cost_per_1k_completion: float,
        operation: str = "chat",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "SessionTypeCostRecord":
        total = prompt_tokens + completion_tokens
        cost = (
            prompt_tokens * cost_per_1k_prompt
            + completion_tokens * cost_per_1k_completion
        ) / 1000.0
        return cls(
            session_type=session_type,
            session_id=session_id,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total,
            cost_usd=round(cost, 6),
            operation=operation,
            metadata=metadata or {},
        )
```

## Solution 2: Per-Session-Type Cost Accumulator

```python
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, List


class PerSessionTypeCostAccumulator:
    """
    Accumulates cost records in a sliding time window, grouped by session type.
    Computes cost-per-session and tokens-per-session for each type.
    """

    def __init__(self, window_seconds: float = 86400.0):
        self._window = window_seconds
        self._records: Deque[SessionTypeCostRecord] = deque()
        self._session_sets: Dict[str, set] = defaultdict(set)
        self._lock = threading.Lock()

    def record(self, rec: SessionTypeCostRecord) -> None:
        with self._lock:
            self._records.append(rec)
            self._session_sets[rec.session_type].add(rec.session_id)
            self._trim()

    def _trim(self) -> None:
        cutoff = time.time() - self._window
        while self._records and self._records[0].recorded_at < cutoff:
            removed = self._records.popleft()
            # Rebuild session sets on trim (approximate — good enough for metrics)

    def snapshot_by_type(self) -> Dict[str, dict]:
        with self._lock:
            self._trim()
            records = list(self._records)

        by_type: Dict[str, List[SessionTypeCostRecord]] = defaultdict(list)
        for r in records:
            by_type[r.session_type].append(r)

        result = {}
        for stype, type_records in by_type.items():
            total_cost = sum(r.cost_usd for r in type_records)
            total_tokens = sum(r.total_tokens for r in type_records)
            unique_sessions = len({r.session_id for r in type_records})
            call_count = len(type_records)
            result[stype] = {
                "session_type": stype,
                "call_count": call_count,
                "unique_sessions": unique_sessions,
                "total_cost_usd": round(total_cost, 4),
                "total_tokens": total_tokens,
                "cost_per_session_usd": round(total_cost / max(unique_sessions, 1), 4),
                "tokens_per_session": round(total_tokens / max(unique_sessions, 1), 1),
                "cost_per_call_usd": round(total_cost / max(call_count, 1), 6),
                "avg_tokens_per_call": round(total_tokens / max(call_count, 1), 1),
            }
        return result

    def total_cost_usd(self) -> float:
        with self._lock:
            self._trim()
            return round(sum(r.cost_usd for r in self._records), 4)
```

## Solution 3: Session Type Cost Baseline Tracker

```python
import time
from collections import deque
from typing import Deque, Dict, List, Optional


class SessionTypeCostBaselineTracker:
    """
    Tracks rolling baselines for cost-per-session per session type.
    Used for anomaly detection: a type whose cost-per-session spikes
    3× above baseline warrants investigation.
    """

    def __init__(self, baseline_window_seconds: float = 7 * 86400.0):  # 7 days
        self._window = baseline_window_seconds
        self._daily_snapshots: Deque[dict] = deque()   # one snapshot per day
        self._last_snapshot_at: float = 0

    def add_daily_snapshot(self, snapshot: Dict[str, dict]) -> None:
        self._daily_snapshots.append({
            "ts": time.time(),
            "data": snapshot,
        })
        cutoff = time.time() - self._window
        while self._daily_snapshots and self._daily_snapshots[0]["ts"] < cutoff:
            self._daily_snapshots.popleft()

    def baseline_cost_per_session(self, session_type: str) -> Optional[float]:
        values = []
        for snap in self._daily_snapshots:
            entry = snap["data"].get(session_type)
            if entry and entry.get("unique_sessions", 0) >= 5:
                values.append(entry["cost_per_session_usd"])
        if not values:
            return None
        return round(sorted(values)[len(values) // 2], 6)  # median

    def anomaly_check(
        self,
        current_snapshot: Dict[str, dict],
        multiplier: float = 3.0,
    ) -> List[dict]:
        anomalies = []
        for stype, current in current_snapshot.items():
            if current.get("unique_sessions", 0) < 5:
                continue
            baseline = self.baseline_cost_per_session(stype)
            if baseline is None or baseline <= 0:
                continue
            current_cps = current.get("cost_per_session_usd", 0)
            if current_cps > baseline * multiplier:
                anomalies.append({
                    "session_type": stype,
                    "current_cost_per_session": current_cps,
                    "baseline_cost_per_session": baseline,
                    "ratio": round(current_cps / baseline, 2),
                    "threshold": multiplier,
                })
        return anomalies
```

## Solution 4: Cost Breakdown Report Generator

```python
import time
from typing import Dict, List


class CostBreakdownReportGenerator:
    """
    Generates structured cost breakdown reports with rankings,
    percentage shares, and trend indicators.
    """

    def __init__(self, accumulator: PerSessionTypeCostAccumulator):
        self._accumulator = accumulator

    def generate(self) -> dict:
        snapshot = self._accumulator.snapshot_by_type()
        total = self._accumulator.total_cost_usd()

        # Rank by total cost descending
        ranked = sorted(
            snapshot.values(),
            key=lambda x: -x["total_cost_usd"],
        )

        for entry in ranked:
            entry["cost_share_pct"] = round(
                entry["total_cost_usd"] / max(total, 0.0001) * 100, 1
            )

        return {
            "generated_at": time.time(),
            "window_hours": self._accumulator._window / 3600,
            "total_cost_usd": total,
            "session_types": ranked,
            "type_count": len(ranked),
        }
```

## Solution 5: Cost Alert Manager

```python
import time
from typing import Callable, List


class SessionTypeCostAlertManager:
    """
    Fires alerts when a session type's cost-per-session deviates from baseline
    or when a single session type exceeds a budget fraction.
    """

    def __init__(
        self,
        accumulator: PerSessionTypeCostAccumulator,
        baseline_tracker: SessionTypeCostBaselineTracker,
        max_single_type_pct: float = 60.0,
        anomaly_multiplier: float = 3.0,
        cooldown_seconds: float = 3600.0,
    ):
        self._accumulator = accumulator
        self._baseline = baseline_tracker
        self._max_type_pct = max_single_type_pct
        self._multiplier = anomaly_multiplier
        self._cooldown = cooldown_seconds
        self._last_fired: dict = {}
        self._handlers: List[Callable[[dict], None]] = []

    def add_handler(self, fn: Callable[[dict], None]) -> None:
        self._handlers.append(fn)

    def _can_fire(self, key: str) -> bool:
        last = self._last_fired.get(key, 0)
        if time.time() - last >= self._cooldown:
            self._last_fired[key] = time.time()
            return True
        return False

    def _fire(self, alert: dict) -> None:
        for h in self._handlers:
            try:
                h(alert)
            except Exception:
                pass

    def check(self) -> List[dict]:
        snapshot = self._accumulator.snapshot_by_type()
        total = self._accumulator.total_cost_usd()
        alerts = []

        # Anomaly detection
        anomalies = self._baseline.anomaly_check(snapshot, self._multiplier)
        for anomaly in anomalies:
            key = f"anomaly:{anomaly['session_type']}"
            if self._can_fire(key):
                alert = {
                    "type": "cost_per_session_anomaly",
                    "severity": "warning",
                    **anomaly,
                    "message": (
                        f"Session type '{anomaly['session_type']}' cost-per-session is "
                        f"{anomaly['ratio']}× above baseline"
                    ),
                }
                alerts.append(alert)
                self._fire(alert)

        # Concentration check
        if total > 0:
            for stype, data in snapshot.items():
                share = data["total_cost_usd"] / total * 100
                if share > self._max_type_pct and self._can_fire(f"concentration:{stype}"):
                    alert = {
                        "type": "cost_concentration",
                        "severity": "warning",
                        "session_type": stype,
                        "cost_share_pct": round(share, 1),
                        "threshold_pct": self._max_type_pct,
                        "message": f"'{stype}' accounts for {share:.1f}% of all LLM cost",
                    }
                    alerts.append(alert)
                    self._fire(alert)

        return alerts
```

## Solution 6: Cost Per Session Type Dashboard

```python
import time


class CostPerSessionTypeDashboard:
    """Combines cost breakdown, baseline comparison, and alerts."""

    def __init__(
        self,
        report_generator: CostBreakdownReportGenerator,
        alert_manager: SessionTypeCostAlertManager,
        baseline_tracker: SessionTypeCostBaselineTracker,
    ):
        self._report = report_generator
        self._alerts = alert_manager
        self._baseline = baseline_tracker

    def render(self) -> dict:
        report = self._report.generate()
        alerts = self._alerts.check()

        # Attach baseline to each session type entry
        for entry in report["session_types"]:
            stype = entry["session_type"]
            baseline = self._baseline.baseline_cost_per_session(stype)
            entry["baseline_cost_per_session"] = baseline
            if baseline and baseline > 0:
                entry["vs_baseline_pct"] = round(
                    (entry["cost_per_session_usd"] / baseline - 1) * 100, 1
                )

        return {
            "generated_at": time.time(),
            "cost_breakdown": report,
            "alerts": alerts,
            "healthy": len(alerts) == 0,
        }
```

## Comparison

| Approach | Per-Type Aggregation | Unit Economics | Baseline Tracking | Anomaly Detection | Alerts |
|---|---|---|---|---|---|
| PerSessionTypeCostAccumulator | Yes | Yes (cost/session) | No | No | No |
| SessionTypeCostBaselineTracker | No | No | Yes (7-day median) | Yes | No |
| CostBreakdownReportGenerator | Via accumulator | Yes (ranked + share) | No | No | No |
| SessionTypeCostAlertManager | Via accumulator | No | Via baseline | Via baseline | Yes |
| CostPerSessionTypeDashboard | No | No | No | No | Yes |

**Best for production**: Tag every LLM call at the call site with `session_type` — this is a single-field addition that unlocks all downstream cost attribution. Define session types at the product level (support, onboarding, search, generation) rather than the technical level (chat, embed) so the cost breakdown maps directly to business metrics. Take a daily snapshot with `SessionTypeCostBaselineTracker.add_daily_snapshot()` to build a 7-day rolling baseline. Alert when any session type's cost-per-session ratio exceeds 3× baseline — this almost always indicates either a new prompt that is significantly longer, a regression in tool call efficiency, or an abuse pattern targeting that workflow.
