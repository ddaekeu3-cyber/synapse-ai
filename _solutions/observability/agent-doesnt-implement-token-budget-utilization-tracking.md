---
title: "Agent Doesn't Implement Token Budget Utilization Tracking"
description: "Agents that do not track how their token budget is spent across system prompts, conversation history, tool results, and model responses have no visibility into why context windows fill up or which component is consuming the most space. Implement token budget utilization tracking that measures per-component token consumption in every request, computes utilization rates, and alerts when any component exceeds its intended share of the budget."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-token-budget-utilization-tracking
tags: [token-budget, context-utilization, token-tracking, context-window, budget-breakdown, token-efficiency]
symptoms:
  - "Context window fills unexpectedly — no visibility into which component consumed the space"
  - "System prompt size creep goes undetected until requests start failing with context overflow"
  - "Tool results sometimes consume 90% of the budget with no alert until the model truncates"
  - "No per-component breakdown of token usage — only total token count is logged"
  - "Cannot determine whether to optimize the system prompt, tool results, or conversation history"
---

## Why This Happens

LLM APIs report total token counts but not where those tokens came from. When a request approaches the context limit, the agent cannot tell whether the system prompt grew, tool results are too large, conversation history is too long, or the model's own response is unusually verbose. Without per-component measurement, optimization is guesswork. Token budget tracking requires instrumenting each component that contributes to the context — system prompt, history, tool results, response — measuring the token count of each before the call, and recording the breakdown so that trends are visible over time.

## Solution 1: Token Budget Allocation

```python
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class TokenBudgetAllocation:
    total_budget: int
    system_prompt_limit: Optional[int] = None
    history_limit: Optional[int] = None
    tool_results_limit: Optional[int] = None
    response_limit: Optional[int] = None

    def __post_init__(self) -> None:
        # Apply sensible defaults if not specified
        if self.system_prompt_limit is None:
            self.system_prompt_limit = int(self.total_budget * 0.15)
        if self.history_limit is None:
            self.history_limit = int(self.total_budget * 0.35)
        if self.tool_results_limit is None:
            self.tool_results_limit = int(self.total_budget * 0.30)
        if self.response_limit is None:
            self.response_limit = int(self.total_budget * 0.20)

    def utilization_fractions(self, usage: "TokenUsageBreakdown") -> Dict[str, float]:
        return {
            "system_prompt": usage.system_prompt / max(self.system_prompt_limit, 1),
            "history": usage.history / max(self.history_limit, 1),
            "tool_results": usage.tool_results / max(self.tool_results_limit, 1),
            "response": usage.response / max(self.response_limit, 1),
            "total": usage.total() / max(self.total_budget, 1),
        }
```

## Solution 2: Token Usage Breakdown

```python
import time
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class TokenUsageBreakdown:
    request_id: str
    session_id: str
    system_prompt: int = 0
    history: int = 0
    tool_results: int = 0
    user_message: int = 0
    response: int = 0
    overhead: int = 0          # formatting, separators, special tokens
    recorded_at: float = field(default_factory=time.time)
    metadata: Dict[str, int] = field(default_factory=dict)  # per-tool breakdown

    def total(self) -> int:
        return self.system_prompt + self.history + self.tool_results + self.user_message + self.response + self.overhead

    def input_total(self) -> int:
        return self.system_prompt + self.history + self.tool_results + self.user_message + self.overhead

    def utilization_pct(self, budget: int) -> float:
        return round(self.total() / max(budget, 1) * 100, 1)

    def largest_component(self) -> str:
        components = {
            "system_prompt": self.system_prompt,
            "history": self.history,
            "tool_results": self.tool_results,
            "user_message": self.user_message,
            "response": self.response,
        }
        return max(components, key=components.get)
```

## Solution 3: Token Counter

```python
from typing import Any, Dict, List, Optional


class TokenCounter:
    """
    Estimates token counts for text components.
    Uses a character-based heuristic by default; replace with
    a real tokenizer (tiktoken, transformers) for production accuracy.
    """

    def __init__(self, chars_per_token: float = 4.0):
        self._ratio = chars_per_token

    def count(self, text: str) -> int:
        if not text:
            return 0
        return max(1, int(len(text) / self._ratio))

    def count_messages(self, messages: List[Dict[str, Any]]) -> int:
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += self.count(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        total += self.count(str(block.get("text", "")))
            total += 4  # message overhead (role, formatting)
        return total

    def count_tool_results(self, results: List[Dict[str, Any]]) -> Dict[str, int]:
        per_tool = {}
        for result in results:
            tool_name = result.get("tool_name", "unknown")
            content = str(result.get("content", ""))
            per_tool[tool_name] = self.count(content)
        return per_tool
```

## Solution 4: Token Budget Tracker

```python
import threading
import time
from collections import deque
from typing import Deque, Dict, List, Optional, Tuple


class TokenBudgetTracker:
    """
    Records token usage breakdowns across requests and computes
    per-component utilization trends and budget violation counts.
    """

    def __init__(
        self,
        allocation: TokenBudgetAllocation,
        max_records: int = 10000,
    ):
        self._allocation = allocation
        self._max = max_records
        self._records: Deque[Tuple[float, TokenUsageBreakdown]] = deque()
        self._lock = threading.Lock()
        self._violations: Dict[str, int] = {
            "system_prompt": 0,
            "history": 0,
            "tool_results": 0,
            "total": 0,
        }

    def record(self, breakdown: TokenUsageBreakdown) -> List[str]:
        """Record usage and return list of budget violation component names."""
        fractions = self._allocation.utilization_fractions(breakdown)
        violations = [comp for comp, frac in fractions.items() if frac > 1.0]

        with self._lock:
            self._records.append((time.time(), breakdown))
            if len(self._records) > self._max:
                self._records.popleft()
            for v in violations:
                self._violations[v] = self._violations.get(v, 0) + 1

        return violations

    def recent_avg(self, component: str, window_seconds: float = 3600.0) -> Optional[float]:
        cutoff = time.time() - window_seconds
        with self._lock:
            values = [
                getattr(b, component, 0)
                for ts, b in self._records
                if ts >= cutoff and hasattr(b, component)
            ]
        return round(sum(values) / len(values), 1) if values else None

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [b for ts, b in self._records if ts >= cutoff]

        if not recent:
            return {"window_seconds": window_seconds, "requests": 0}

        components = ["system_prompt", "history", "tool_results", "user_message", "response"]
        avg_by_component = {
            comp: round(sum(getattr(b, comp, 0) for b in recent) / len(recent), 1)
            for comp in components
        }

        totals = [b.total() for b in recent]
        utilizations = [b.utilization_pct(self._allocation.total_budget) for b in recent]
        largest = [b.largest_component() for b in recent]
        from collections import Counter
        largest_counts = Counter(largest)

        return {
            "window_seconds": window_seconds,
            "requests": len(recent),
            "avg_tokens_by_component": avg_by_component,
            "avg_total_tokens": round(sum(totals) / len(recent), 1),
            "avg_utilization_pct": round(sum(utilizations) / len(recent), 1),
            "p95_total_tokens": sorted(totals)[min(int(len(totals) * 0.95), len(totals) - 1)],
            "most_common_largest_component": largest_counts.most_common(1)[0] if largest_counts else None,
            "budget_violations": dict(self._violations),
        }
```

## Solution 5: Budget Utilization Alerter

```python
from dataclasses import dataclass
from typing import List


@dataclass
class BudgetAlertThresholds:
    warn_utilization_pct: float = 80.0
    critical_utilization_pct: float = 95.0
    system_prompt_warn_tokens: int = 2000
    tool_results_warn_pct: float = 60.0   # percent of total budget


class BudgetUtilizationAlerter:
    """
    Evaluates a token usage breakdown against alert thresholds
    and returns structured alerts for any exceeded threshold.
    """

    def __init__(
        self,
        allocation: TokenBudgetAllocation,
        thresholds: BudgetAlertThresholds = None,
    ):
        self._allocation = allocation
        self._thresholds = thresholds or BudgetAlertThresholds()

    def check(self, breakdown: TokenUsageBreakdown) -> List[dict]:
        alerts = []
        total = breakdown.total()
        budget = self._allocation.total_budget
        utilization = total / max(budget, 1) * 100

        if utilization >= self._thresholds.critical_utilization_pct:
            alerts.append({
                "severity": "critical",
                "component": "total",
                "message": f"total token utilization {utilization:.1f}% exceeds critical threshold",
                "value": total,
                "limit": budget,
            })
        elif utilization >= self._thresholds.warn_utilization_pct:
            alerts.append({
                "severity": "warning",
                "component": "total",
                "message": f"total token utilization {utilization:.1f}% exceeds warn threshold",
                "value": total,
                "limit": budget,
            })

        if breakdown.system_prompt > self._thresholds.system_prompt_warn_tokens:
            alerts.append({
                "severity": "warning",
                "component": "system_prompt",
                "message": f"system prompt {breakdown.system_prompt} tokens exceeds {self._thresholds.system_prompt_warn_tokens}",
                "value": breakdown.system_prompt,
                "limit": self._thresholds.system_prompt_warn_tokens,
            })

        tool_pct = breakdown.tool_results / max(budget, 1) * 100
        if tool_pct >= self._thresholds.tool_results_warn_pct:
            alerts.append({
                "severity": "warning",
                "component": "tool_results",
                "message": f"tool results consuming {tool_pct:.1f}% of total budget",
                "value": breakdown.tool_results,
                "limit": int(budget * self._thresholds.tool_results_warn_pct / 100),
            })

        return alerts
```

## Solution 6: Token Budget Dashboard

```python
import time


class TokenBudgetDashboard:
    """
    Combines budget allocation, historical usage trends, and
    live alert evaluation into a single operational view.
    """

    def __init__(
        self,
        allocation: TokenBudgetAllocation,
        tracker: TokenBudgetTracker,
        alerter: BudgetUtilizationAlerter,
    ):
        self._allocation = allocation
        self._tracker = tracker
        self._alerter = alerter

    def render(self, latest_breakdown: Optional[TokenUsageBreakdown] = None) -> dict:
        report = {
            "generated_at": time.time(),
            "budget": {
                "total": self._allocation.total_budget,
                "system_prompt_limit": self._allocation.system_prompt_limit,
                "history_limit": self._allocation.history_limit,
                "tool_results_limit": self._allocation.tool_results_limit,
                "response_limit": self._allocation.response_limit,
            },
            "historical": self._tracker.summary(window_seconds=3600.0),
        }

        if latest_breakdown:
            report["latest_breakdown"] = {
                "system_prompt": latest_breakdown.system_prompt,
                "history": latest_breakdown.history,
                "tool_results": latest_breakdown.tool_results,
                "user_message": latest_breakdown.user_message,
                "response": latest_breakdown.response,
                "total": latest_breakdown.total(),
                "utilization_pct": latest_breakdown.utilization_pct(self._allocation.total_budget),
                "largest_component": latest_breakdown.largest_component(),
            }
            report["alerts"] = self._alerter.check(latest_breakdown)

        return report
```

## Comparison

| Approach | Per-Component Breakdown | Budget Allocation | Trend Tracking | Alert Generation | Dashboard |
|---|---|---|---|---|---|
| TokenUsageBreakdown | Yes (dataclass) | No | No | No | No |
| TokenCounter | No | No | No | No | No |
| TokenBudgetTracker | Via breakdown | Via allocation | Yes (sliding) | No | No |
| BudgetUtilizationAlerter | Via breakdown | Via allocation | No | Yes | No |
| TokenBudgetDashboard | No | Via allocation | Via tracker | Via alerter | Yes |

**Best for production**: Replace the character-based `TokenCounter` heuristic with `tiktoken` for OpenAI models or the model provider's native tokenizer — character ratios vary significantly by language and content type. Track `system_prompt` tokens separately with a high-visibility alert: system prompt growth is typically caused by feature additions that are never reviewed for token impact, and 2,000+ token system prompts are a common cause of unexpected context overflow. Alert when `tool_results` exceeds 60% of the total budget — this is the most common cause of context pressure and should trigger result truncation or pagination.
