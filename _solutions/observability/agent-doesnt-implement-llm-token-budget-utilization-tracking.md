---
title: "Agent Doesn't Implement LLM Token Budget Utilization Tracking"
description: "Agents that do not track token budget utilization per request have no visibility into how close they are to context limits, which components consume the most tokens, or whether context overflow is silently truncating content. Without utilization tracking, engineers cannot optimize context assembly, detect runaway prompts, or alert on near-limit conditions before they cause failures. Implement per-request token budget utilization tracking that breaks down consumption by component and monitors for budget exhaustion patterns."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-llm-token-budget-utilization-tracking
tags: [token-budget, utilization-tracking, context-window, component-breakdown, budget-monitoring, context-overflow]
symptoms:
  - "No visibility into how much of the context window is used per request"
  - "Silent context truncation discovered only when LLM answers incorrectly"
  - "Cannot identify which component (system prompt, history, tools) consumes the most tokens"
  - "No alert when a request is near the context window limit"
  - "Token usage trends are invisible — no way to know if context pressure is increasing"
---

## Why This Happens

LLM API responses include total token counts but not a breakdown by component. The application knows it sent 45,000 tokens but not that 12,000 came from the system prompt, 18,000 from conversation history, 8,000 from tool results, and 7,000 from retrieved documents. Without this breakdown, optimization is guesswork. Component-level tracking requires instrumenting the context assembly step — before the LLM call — where each component's text is available separately, so token estimates can be attributed accurately.

## Solution 1: Token Budget Allocation

```python
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class TokenBudgetAllocation:
    """
    Defines the intended token budget for each context component.
    Actual usage is measured and compared against these allocations.
    """
    total_window: int
    system_prompt: int
    conversation_history: int
    tool_results: int
    retrieved_context: int
    generation_reserve: int
    overhead: int = 200    # JSON structure, role labels, etc.

    def allocated_input(self) -> int:
        return (
            self.system_prompt
            + self.conversation_history
            + self.tool_results
            + self.retrieved_context
            + self.overhead
        )

    def utilization(self) -> float:
        return round(self.allocated_input() / max(self.total_window, 1), 4)
```

## Solution 2: Context Component Measurer

```python
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class ComponentTokenCount:
    component: str
    char_count: int
    estimated_tokens: int
    pct_of_window: float


class ContextComponentMeasurer:
    """
    Estimates token counts for each context component before the LLM call.
    Uses character-based estimation with model-specific calibration.
    """

    def __init__(
        self,
        model_window: int = 128000,
        tokens_per_char: float = 0.25,
    ):
        self._window = model_window
        self._tpc = tokens_per_char

    def _estimate(self, text: str) -> int:
        return max(1, int(len(text) * self._tpc))

    def measure(
        self,
        system_prompt: str = "",
        conversation_history: List[str] = None,
        tool_results: List[str] = None,
        retrieved_chunks: List[str] = None,
        user_message: str = "",
    ) -> List[ComponentTokenCount]:
        def pct(tokens: int) -> float:
            return round(tokens / self._window * 100, 2)

        history_text = "\n".join(conversation_history or [])
        tool_text = "\n".join(tool_results or [])
        retrieval_text = "\n".join(retrieved_chunks or [])

        components = [
            ("system_prompt", system_prompt),
            ("conversation_history", history_text),
            ("tool_results", tool_text),
            ("retrieved_context", retrieval_text),
            ("user_message", user_message),
        ]

        return [
            ComponentTokenCount(
                component=name,
                char_count=len(text),
                estimated_tokens=self._estimate(text),
                pct_of_window=pct(self._estimate(text)),
            )
            for name, text in components
        ]
```

## Solution 3: Token Budget Utilization Record

```python
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class TokenBudgetUtilizationRecord:
    session_id: str
    turn_index: int
    model_id: str
    window_size: int
    recorded_at: float

    # Pre-call estimates
    component_estimates: List[ComponentTokenCount]
    total_estimated_input: int
    estimated_utilization_pct: float

    # Post-call actuals (from API response)
    actual_input_tokens: Optional[int] = None
    actual_output_tokens: Optional[int] = None
    actual_total_tokens: Optional[int] = None
    near_limit: bool = False          # True if > 85% of window used
    over_limit: bool = False          # True if > 100% (truncation likely)

    def component_breakdown(self) -> Dict[str, int]:
        return {c.component: c.estimated_tokens for c in self.component_estimates}

    def dominant_component(self) -> str:
        if not self.component_estimates:
            return "unknown"
        return max(self.component_estimates, key=lambda c: c.estimated_tokens).component
```

## Solution 4: Budget Utilization Tracker

```python
import time
from collections import defaultdict
from threading import Lock
from typing import Dict, List, Optional


class TokenBudgetUtilizationTracker:
    """
    Records per-request token budget utilization and provides
    aggregated statistics for monitoring and alerting.
    """

    def __init__(
        self,
        model_window: int = 128000,
        near_limit_threshold: float = 0.85,
        max_records: int = 50_000,
    ):
        self._window = model_window
        self._near_limit = near_limit_threshold
        self._max = max_records
        self._lock = Lock()
        self._records: List[TokenBudgetUtilizationRecord] = []

    def record(self, record: TokenBudgetUtilizationRecord) -> None:
        record.near_limit = (record.estimated_utilization_pct / 100) >= self._near_limit
        record.over_limit = (record.estimated_utilization_pct / 100) >= 1.0
        with self._lock:
            if len(self._records) >= self._max:
                self._records.pop(0)
            self._records.append(record)

    def update_actuals(
        self,
        session_id: str,
        turn_index: int,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        with self._lock:
            for r in reversed(self._records):
                if r.session_id == session_id and r.turn_index == turn_index:
                    r.actual_input_tokens = input_tokens
                    r.actual_output_tokens = output_tokens
                    r.actual_total_tokens = input_tokens + output_tokens
                    break

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for r in self._records if r.recorded_at >= cutoff]

        if not recent:
            return {"window_seconds": window_seconds, "requests": 0}

        avg_util = sum(r.estimated_utilization_pct for r in recent) / len(recent)
        near_limit_count = sum(1 for r in recent if r.near_limit)
        over_limit_count = sum(1 for r in recent if r.over_limit)

        component_totals: Dict[str, List[int]] = defaultdict(list)
        for r in recent:
            for c in r.component_estimates:
                component_totals[c.component].append(c.estimated_tokens)

        return {
            "window_seconds": window_seconds,
            "requests": len(recent),
            "avg_utilization_pct": round(avg_util, 2),
            "near_limit_rate": round(near_limit_count / len(recent), 4),
            "over_limit_count": over_limit_count,
            "avg_tokens_by_component": {
                comp: round(sum(vals) / len(vals), 1)
                for comp, vals in component_totals.items()
            },
        }
```

## Solution 5: Budget Pressure Alert

```python
import time
from typing import Callable, List, Optional


class TokenBudgetPressureAlert:
    """
    Fires an alert when token budget utilization stays above
    the threshold for too many consecutive requests.
    """

    def __init__(
        self,
        tracker: TokenBudgetUtilizationTracker,
        alert_fn: Optional[Callable[[dict], None]] = None,
        consecutive_threshold: int = 5,
        utilization_threshold_pct: float = 85.0,
        cooldown_seconds: float = 300.0,
    ):
        self._tracker = tracker
        self._alert_fn = alert_fn or self._default_alert
        self._consec_threshold = consecutive_threshold
        self._util_threshold = utilization_threshold_pct
        self._cooldown = cooldown_seconds
        self._last_alert_at: float = 0.0

    @staticmethod
    def _default_alert(payload: dict) -> None:
        import json
        print(f"[TOKEN_BUDGET_ALERT] {json.dumps(payload)}")

    def check(self, record: TokenBudgetUtilizationRecord) -> None:
        if record.estimated_utilization_pct < self._util_threshold:
            return
        if time.time() - self._last_alert_at < self._cooldown:
            return
        summary = self._tracker.summary(window_seconds=600.0)
        if summary.get("near_limit_rate", 0) >= (self._consec_threshold / max(summary.get("requests", 1), 1)):
            self._last_alert_at = time.time()
            self._alert_fn({
                "event": "token_budget_pressure",
                "near_limit_rate": summary["near_limit_rate"],
                "avg_utilization_pct": summary["avg_utilization_pct"],
                "dominant_component": record.dominant_component(),
                "model_window": self._tracker._window,
            })
```

## Solution 6: Token Budget Dashboard

```python
import time


class TokenBudgetUtilizationDashboard:
    """
    Renders a full token budget utilization picture:
    component breakdown, utilization trends, and pressure alerts.
    """

    def __init__(
        self,
        tracker: TokenBudgetUtilizationTracker,
        measurer: ContextComponentMeasurer,
    ):
        self._tracker = tracker
        self._measurer = measurer

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "model_window_tokens": self._tracker._window,
            "near_limit_threshold_pct": self._tracker._near_limit * 100,
            "last_hour": self._tracker.summary(window_seconds=3600.0),
            "last_24h": self._tracker.summary(window_seconds=86400.0),
        }
```

## Comparison

| Approach | Component Breakdown | Pre-Call Estimate | Post-Call Actuals | Near-Limit Alert | Dashboard |
|---|---|---|---|---|---|
| ContextComponentMeasurer | Yes (5 components) | Yes | No | No | No |
| TokenBudgetUtilizationTracker | Via measurer | Yes | Yes (update) | Via threshold | No |
| TokenBudgetPressureAlert | No | No | No | Yes (consecutive) | No |
| TokenBudgetUtilizationDashboard | No | No | No | No | Yes |

**Best for production**: Record component estimates before every LLM call and update with actuals from the API response immediately after — the delta between estimated and actual tokens reveals how well your character-based estimator is calibrated. Alert when `near_limit_rate > 0.20` over a 10-minute window: one in five requests near the context ceiling is a signal to either reduce context payload sizes or increase the model's context window. Use `dominant_component` to direct optimization effort: if `retrieved_context` dominates, tighten retrieval chunk counts; if `conversation_history` dominates, trigger summarization earlier.
