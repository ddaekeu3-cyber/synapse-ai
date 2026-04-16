---
title: "Agent Doesn't Implement Tool Call Success Rate Tracking Per Model"
description: "Agents that route requests to multiple LLM models have no visibility into which models produce parseable tool calls reliably. One model may generate malformed JSON in 15% of tool calls while another fails to select the correct tool 8% of the time. Implement per-model tool call success rate tracking that measures parse failures, wrong-tool selections, and schema violations, and alerts when a model's success rate drops below threshold."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-tool-call-success-rate-tracking-per-model
tags: [tool-call-tracking, model-comparison, success-rate, parse-failure, schema-validation, multi-model]
symptoms:
  - "No data on which model produces valid tool call JSON most reliably"
  - "Parse errors from tool calls are caught and retried but never aggregated by model"
  - "Model routing decisions are made on latency alone with no quality signal"
  - "A newly deployed model has 20% tool call failure rate but alerts don't fire for days"
  - "Cannot answer 'which model should we use for tool-heavy workflows?' from observability data"
---

## Why This Happens

Tool call success is binary at the call site — it either parses or it doesn't — and the failure is handled locally with a retry. The aggregate pattern (this model fails 12% of the time, that model 2%) is never assembled because each failure is logged independently without grouping by model. Per-model tracking requires recording every tool call attempt with its model identifier and outcome, computing success rates in a sliding window, and comparing rates across models. This turns anecdotal "model X seems flaky" into a quantified signal.

## Solution 1: Tool Call Outcome

```python
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ToolCallOutcome(str, Enum):
    SUCCESS = "success"                    # tool called and returned a result
    PARSE_FAILURE = "parse_failure"        # LLM output was not valid JSON
    SCHEMA_VIOLATION = "schema_violation"  # JSON valid but failed schema check
    WRONG_TOOL = "wrong_tool"              # LLM called a tool not in the registry
    NO_TOOL_CALL = "no_tool_call"          # LLM didn't emit a tool call when expected
    TOOL_ERROR = "tool_error"              # tool executed but raised an exception
    TIMEOUT = "timeout"                    # tool call exceeded timeout


@dataclass
class ToolCallRecord:
    record_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    model: str = ""
    tool_name: str = ""
    outcome: ToolCallOutcome = ToolCallOutcome.SUCCESS
    latency_ms: float = 0.0
    error_detail: Optional[str] = None
    session_id: str = ""
    recorded_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_success(self) -> bool:
        return self.outcome == ToolCallOutcome.SUCCESS

    def is_model_fault(self) -> bool:
        """True if the failure was caused by the model's output quality."""
        return self.outcome in (
            ToolCallOutcome.PARSE_FAILURE,
            ToolCallOutcome.SCHEMA_VIOLATION,
            ToolCallOutcome.WRONG_TOOL,
            ToolCallOutcome.NO_TOOL_CALL,
        )
```

## Solution 2: Per-Model Success Rate Accumulator

```python
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Optional


@dataclass
class ModelToolCallStats:
    model: str
    window_seconds: float
    _records: Deque = field(default_factory=deque)
    _lock: object = field(default_factory=threading.Lock)

    def record(self, rec: ToolCallRecord) -> None:
        with self._lock:
            self._records.append(rec)
            self._trim()

    def _trim(self) -> None:
        cutoff = time.time() - self.window_seconds
        while self._records and self._records[0].recorded_at < cutoff:
            self._records.popleft()

    def snapshot(self) -> dict:
        with self._lock:
            self._trim()
            records = list(self._records)

        if not records:
            return {"model": self.model, "calls": 0}

        total = len(records)
        successes = sum(1 for r in records if r.is_success())
        model_faults = sum(1 for r in records if r.is_model_fault())
        by_outcome: Dict[str, int] = {}
        by_tool: Dict[str, Dict[str, int]] = {}

        for r in records:
            by_outcome[r.outcome] = by_outcome.get(r.outcome, 0) + 1
            if r.tool_name:
                tool_stats = by_tool.setdefault(r.tool_name, {"total": 0, "success": 0})
                tool_stats["total"] += 1
                if r.is_success():
                    tool_stats["success"] += 1

        avg_latency = sum(r.latency_ms for r in records) / total

        return {
            "model": self.model,
            "calls": total,
            "successes": successes,
            "success_rate": round(successes / total, 4),
            "model_fault_rate": round(model_faults / total, 4),
            "avg_latency_ms": round(avg_latency, 2),
            "by_outcome": by_outcome,
            "per_tool_success_rates": {
                tool: round(s["success"] / max(s["total"], 1), 4)
                for tool, s in by_tool.items()
            },
        }


class PerModelSuccessRateAccumulator:
    """Registry of per-model stats accumulators."""

    def __init__(self, window_seconds: float = 3600.0):
        self._window = window_seconds
        self._models: Dict[str, ModelToolCallStats] = {}
        self._lock = threading.Lock()

    def record(self, rec: ToolCallRecord) -> None:
        with self._lock:
            if rec.model not in self._models:
                self._models[rec.model] = ModelToolCallStats(
                    model=rec.model,
                    window_seconds=self._window,
                )
        self._models[rec.model].record(rec)

    def snapshot(self, model: str) -> Optional[dict]:
        stats = self._models.get(model)
        return stats.snapshot() if stats else None

    def all_snapshots(self) -> List[dict]:
        return [s.snapshot() for s in self._models.values()]
```

## Solution 3: Tool Call Outcome Recorder

```python
import asyncio
import time
from typing import Any, Callable, Dict, Optional


class ToolCallOutcomeRecorder:
    """
    Wraps tool dispatch to automatically record outcomes.
    Distinguishes model-fault failures (bad LLM output) from
    infrastructure failures (tool raised an exception).
    """

    def __init__(self, accumulator: PerModelSuccessRateAccumulator):
        self._acc = accumulator

    async def record_dispatch(
        self,
        model: str,
        tool_name: str,
        tool_fn: Optional[Callable],
        raw_tool_args: Optional[Dict[str, Any]],
        session_id: str = "",
        parse_error: Optional[str] = None,
        schema_error: Optional[str] = None,
    ) -> Any:
        start = time.time()

        if parse_error:
            self._acc.record(ToolCallRecord(
                model=model, tool_name=tool_name,
                outcome=ToolCallOutcome.PARSE_FAILURE,
                error_detail=parse_error[:200],
                session_id=session_id,
                latency_ms=(time.time() - start) * 1000,
            ))
            raise ValueError(f"Tool call parse failure: {parse_error}")

        if schema_error:
            self._acc.record(ToolCallRecord(
                model=model, tool_name=tool_name,
                outcome=ToolCallOutcome.SCHEMA_VIOLATION,
                error_detail=schema_error[:200],
                session_id=session_id,
                latency_ms=(time.time() - start) * 1000,
            ))
            raise ValueError(f"Tool call schema violation: {schema_error}")

        if tool_fn is None:
            self._acc.record(ToolCallRecord(
                model=model, tool_name=tool_name,
                outcome=ToolCallOutcome.WRONG_TOOL,
                session_id=session_id,
                latency_ms=(time.time() - start) * 1000,
            ))
            raise KeyError(f"Unknown tool: {tool_name}")

        try:
            result = await asyncio.wait_for(
                tool_fn(**(raw_tool_args or {})),
                timeout=30.0,
            )
            self._acc.record(ToolCallRecord(
                model=model, tool_name=tool_name,
                outcome=ToolCallOutcome.SUCCESS,
                session_id=session_id,
                latency_ms=(time.time() - start) * 1000,
            ))
            return result
        except asyncio.TimeoutError:
            self._acc.record(ToolCallRecord(
                model=model, tool_name=tool_name,
                outcome=ToolCallOutcome.TIMEOUT,
                session_id=session_id,
                latency_ms=(time.time() - start) * 1000,
            ))
            raise
        except Exception as exc:
            self._acc.record(ToolCallRecord(
                model=model, tool_name=tool_name,
                outcome=ToolCallOutcome.TOOL_ERROR,
                error_detail=str(exc)[:200],
                session_id=session_id,
                latency_ms=(time.time() - start) * 1000,
            ))
            raise
```

## Solution 4: Model Ranking Comparator

```python
from typing import List


class ModelToolCallRankingComparator:
    """
    Ranks models by tool call reliability using a weighted score.
    Weights model-fault failures (LLM quality issues) more heavily
    than infrastructure failures (tool errors, timeouts).
    """

    def __init__(
        self,
        accumulator: PerModelSuccessRateAccumulator,
        model_fault_weight: float = 2.0,
    ):
        self._acc = accumulator
        self._weight = model_fault_weight

    def rank(self) -> List[dict]:
        snapshots = self._acc.all_snapshots()
        ranked = []
        for s in snapshots:
            if s.get("calls", 0) < 10:
                continue    # not enough data to rank
            quality_score = (
                s.get("success_rate", 0)
                - s.get("model_fault_rate", 0) * (self._weight - 1)
            )
            ranked.append({
                "model": s["model"],
                "quality_score": round(quality_score, 4),
                "success_rate": s.get("success_rate"),
                "model_fault_rate": s.get("model_fault_rate"),
                "calls": s.get("calls"),
                "avg_latency_ms": s.get("avg_latency_ms"),
            })
        return sorted(ranked, key=lambda x: -x["quality_score"])
```

## Solution 5: Success Rate Alert Manager

```python
import time
from typing import Callable, List


class ToolCallSuccessRateAlertManager:
    """
    Fires alerts when any model's tool call success rate drops below threshold
    or when the model_fault_rate spikes, indicating model quality degradation.
    """

    def __init__(
        self,
        accumulator: PerModelSuccessRateAccumulator,
        min_success_rate: float = 0.90,
        max_fault_rate: float = 0.05,
        min_calls_to_alert: int = 20,
        cooldown_seconds: float = 300.0,
    ):
        self._acc = accumulator
        self._min_success = min_success_rate
        self._max_fault = max_fault_rate
        self._min_calls = min_calls_to_alert
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

    def check(self) -> List[dict]:
        alerts = []
        for snap in self._acc.all_snapshots():
            model = snap["model"]
            calls = snap.get("calls", 0)
            if calls < self._min_calls:
                continue

            sr = snap.get("success_rate", 1.0)
            fr = snap.get("model_fault_rate", 0.0)

            if sr < self._min_success and self._can_fire(f"{model}:low_success"):
                alert = {
                    "type": "low_tool_call_success_rate",
                    "model": model,
                    "success_rate": sr,
                    "threshold": self._min_success,
                    "severity": "critical" if sr < 0.75 else "warning",
                    "message": f"Model '{model}' tool call success rate {sr:.1%} below threshold {self._min_success:.1%}",
                }
                alerts.append(alert)
                for h in self._handlers:
                    try:
                        h(alert)
                    except Exception:
                        pass

            if fr > self._max_fault and self._can_fire(f"{model}:high_fault"):
                alert = {
                    "type": "high_model_fault_rate",
                    "model": model,
                    "fault_rate": fr,
                    "threshold": self._max_fault,
                    "severity": "warning",
                    "message": f"Model '{model}' is producing malformed tool calls at {fr:.1%} rate",
                }
                alerts.append(alert)
                for h in self._handlers:
                    try:
                        h(alert)
                    except Exception:
                        pass

        return alerts
```

## Solution 6: Tool Call Quality Dashboard

```python
import time


class ToolCallQualityDashboard:
    """Combines per-model snapshots, rankings, and alerts."""

    def __init__(
        self,
        accumulator: PerModelSuccessRateAccumulator,
        comparator: ModelToolCallRankingComparator,
        alert_manager: ToolCallSuccessRateAlertManager,
    ):
        self._acc = accumulator
        self._comparator = comparator
        self._alerts = alert_manager

    def render(self) -> dict:
        alerts = self._alerts.check()
        return {
            "generated_at": time.time(),
            "model_snapshots": self._acc.all_snapshots(),
            "model_ranking": self._comparator.rank(),
            "alerts": alerts,
            "healthy": len(alerts) == 0,
        }
```

## Comparison

| Approach | Per-Model Tracking | Outcome Classification | Model Ranking | Alert Firing | Dashboard |
|---|---|---|---|---|---|
| PerModelSuccessRateAccumulator | Yes | Via records | No | No | No |
| ToolCallOutcomeRecorder | No | Yes (6 outcomes) | No | No | No |
| ModelToolCallRankingComparator | Via accumulator | No | Yes (quality score) | No | No |
| ToolCallSuccessRateAlertManager | Via accumulator | No | No | Yes | No |
| ToolCallQualityDashboard | No | No | Yes | Via manager | Yes |

**Best for production**: Instrument `ToolCallOutcomeRecorder` at the single point where tool calls are dispatched — this captures every outcome without changing business logic. Separate `model_fault_rate` from overall failure rate in your dashboards: a high `model_fault_rate` means the LLM is producing bad output (prompt or model issue), while high `TOOL_ERROR` rates mean the tool itself is broken. Set alert thresholds at `min_success_rate=0.90` and `max_fault_rate=0.05` — below 90% tool call success, the agent's reliability degrades noticeably for users. Use `ModelToolCallRankingComparator.rank()` to drive model routing decisions: for tool-heavy workflows, always route to the highest-ranked model.
