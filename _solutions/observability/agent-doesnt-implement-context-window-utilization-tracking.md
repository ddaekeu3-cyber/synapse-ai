---
title: "Agent Doesn't Implement Context Window Utilization Tracking"
description: "Agents that never measure how much of the context window is consumed before each LLM call operate blind: they discover context overflow only when the API returns a 'context length exceeded' error mid-task. Implement context window utilization tracking that estimates token consumption by component (system prompt, conversation history, tool results, injected documents), reports utilization percentage, and triggers preemptive compression or truncation before the limit is hit."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-context-window-utilization-tracking
tags: [context-window, token-tracking, utilization, overflow-prevention, token-budget, llm-observability]
symptoms:
  - "Agent fails mid-task with 'context length exceeded' errors"
  - "No visibility into how much context space each component consumes"
  - "Context overflow discovered at API call time — no preemptive action possible"
  - "Cannot determine whether conversation history or tool results are the primary consumer"
  - "P99 context utilization unknown — no alerting before limits are reached"
---

## Why This Happens

LLM APIs enforce a hard token limit per request. Agents that assemble prompts from multiple sources — system prompt, prior turns, tool results, retrieved documents — have no built-in mechanism to measure cumulative token consumption before the call is made. Without a pre-call utilization estimate, the agent cannot take preemptive action: compressing history, dropping low-priority tool results, or paginating documents. The fix is a token-estimation layer that runs before each API call, breaks down consumption by component, and exposes utilization as a first-class metric for both reactive alerting and proactive budget management.

## Solution 1: Token Estimator

```python
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class TokenEstimate:
    char_count: int
    estimated_tokens: int
    method: str   # "tiktoken" | "char_ratio" | "word_ratio"


class TokenEstimator:
    """
    Estimates token count for a text string.
    Uses tiktoken when available; falls back to character-ratio estimation.
    Character ratio of 4 chars/token is a conservative estimate for English text.
    """

    _CHARS_PER_TOKEN = 4.0

    def __init__(self, model: str = "gpt-4o", use_tiktoken: bool = True):
        self._encoder = None
        self._model = model
        if use_tiktoken:
            try:
                import tiktoken
                self._encoder = tiktoken.encoding_for_model(model)
            except Exception:
                pass

    def estimate(self, text: str) -> TokenEstimate:
        char_count = len(text)
        if self._encoder is not None:
            try:
                tokens = len(self._encoder.encode(text))
                return TokenEstimate(char_count=char_count, estimated_tokens=tokens, method="tiktoken")
            except Exception:
                pass
        estimated = max(1, int(char_count / self._CHARS_PER_TOKEN))
        return TokenEstimate(char_count=char_count, estimated_tokens=estimated, method="char_ratio")

    def estimate_messages(self, messages: List[Dict[str, Any]]) -> int:
        """Estimate tokens for a list of chat messages including role overhead."""
        total = 0
        for msg in messages:
            content = msg.get("content") or ""
            if isinstance(content, list):
                content = " ".join(
                    block.get("text", "") for block in content if isinstance(block, dict)
                )
            total += self.estimate(str(content)).estimated_tokens
            total += 4   # per-message overhead (role, separators)
        total += 2   # reply priming tokens
        return total
```

## Solution 2: Context Component Budget

```python
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class ContextComponentBudget:
    model_context_limit: int          # total token limit for the model
    system_prompt_reserve: int = 0    # tokens reserved for system prompt
    output_reserve: int = 1024        # tokens reserved for model output
    tool_schema_reserve: int = 0      # tokens reserved for tool definitions

    @property
    def available_for_input(self) -> int:
        return (
            self.model_context_limit
            - self.system_prompt_reserve
            - self.output_reserve
            - self.tool_schema_reserve
        )


@dataclass
class ContextUtilizationReport:
    model_context_limit: int
    components: Dict[str, int]        # component_name -> token_count
    total_input_tokens: int
    output_reserve: int
    utilization_pct: float
    headroom_tokens: int
    over_limit: bool
    warnings: list = field(default_factory=list)
```

## Solution 3: Context Window Utilization Tracker

```python
import time
from collections import deque
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple


class ContextWindowUtilizationTracker:
    """
    Measures token consumption by component before each LLM call.
    Records utilization history for trend analysis and alerting.
    """

    def __init__(
        self,
        estimator: TokenEstimator,
        budget: ContextComponentBudget,
        warn_threshold_pct: float = 80.0,
        alert_threshold_pct: float = 95.0,
        max_history: int = 1000,
    ):
        self._estimator = estimator
        self._budget = budget
        self._warn = warn_threshold_pct
        self._alert = alert_threshold_pct
        self._history: deque = deque(maxlen=max_history)
        self._lock = Lock()

    def measure(
        self,
        system_prompt: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
        tool_results: Optional[List[str]] = None,
        injected_documents: Optional[List[str]] = None,
        tool_schemas: Optional[List[str]] = None,
    ) -> ContextUtilizationReport:
        components: Dict[str, int] = {}

        if system_prompt:
            components["system_prompt"] = self._estimator.estimate(system_prompt).estimated_tokens

        if messages:
            components["conversation_history"] = self._estimator.estimate_messages(messages)

        if tool_results:
            components["tool_results"] = sum(
                self._estimator.estimate(r).estimated_tokens for r in tool_results
            )

        if injected_documents:
            components["injected_documents"] = sum(
                self._estimator.estimate(d).estimated_tokens for d in injected_documents
            )

        if tool_schemas:
            components["tool_schemas"] = sum(
                self._estimator.estimate(s).estimated_tokens for s in tool_schemas
            )

        total_input = sum(components.values())
        limit = self._budget.model_context_limit
        utilization_pct = round(total_input / max(limit, 1) * 100, 2)
        headroom = limit - total_input - self._budget.output_reserve
        over_limit = total_input + self._budget.output_reserve > limit

        warnings = []
        if over_limit:
            warnings.append(f"OVER LIMIT: {total_input} input + {self._budget.output_reserve} output > {limit}")
        elif utilization_pct >= self._alert:
            warnings.append(f"CRITICAL: context at {utilization_pct:.1f}% utilization")
        elif utilization_pct >= self._warn:
            warnings.append(f"WARNING: context at {utilization_pct:.1f}% utilization")

        report = ContextUtilizationReport(
            model_context_limit=limit,
            components=components,
            total_input_tokens=total_input,
            output_reserve=self._budget.output_reserve,
            utilization_pct=utilization_pct,
            headroom_tokens=headroom,
            over_limit=over_limit,
            warnings=warnings,
        )

        with self._lock:
            self._history.append((time.time(), utilization_pct, over_limit))

        return report

    def history_summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [(ts, pct, over) for ts, pct, over in self._history if ts >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "calls": 0}
        pcts = [pct for _, pct, _ in recent]
        return {
            "window_seconds": window_seconds,
            "calls": len(recent),
            "mean_utilization_pct": round(sum(pcts) / len(pcts), 2),
            "max_utilization_pct": round(max(pcts), 2),
            "p95_utilization_pct": round(sorted(pcts)[int(len(pcts) * 0.95)], 2),
            "over_limit_count": sum(1 for _, _, over in recent if over),
        }
```

## Solution 4: Per-Component Growth Detector

```python
from typing import Dict, List, Optional, Tuple


class PerComponentGrowthDetector:
    """
    Tracks how each context component grows across consecutive calls within a session.
    Identifies which component is the primary driver of context growth.
    """

    def __init__(self):
        self._snapshots: List[Dict[str, int]] = []

    def record(self, components: Dict[str, int]) -> None:
        self._snapshots.append(dict(components))

    def growth_report(self) -> dict:
        if len(self._snapshots) < 2:
            return {"status": "insufficient_snapshots", "snapshots": len(self._snapshots)}

        first = self._snapshots[0]
        last = self._snapshots[-1]
        growth: Dict[str, int] = {}

        for key in set(first) | set(last):
            growth[key] = last.get(key, 0) - first.get(key, 0)

        fastest_growing = max(growth, key=lambda k: growth[k]) if growth else None

        return {
            "snapshots": len(self._snapshots),
            "growth_by_component": growth,
            "fastest_growing_component": fastest_growing,
            "total_growth_tokens": sum(growth.values()),
        }
```

## Solution 5: Preemptive Overflow Guard

```python
from typing import Any, Callable, Dict, List, Optional


class PreemptiveOverflowGuard:
    """
    Runs the utilization tracker before each LLM call and invokes
    registered compression callbacks when utilization exceeds thresholds.
    Callbacks are invoked in priority order until utilization is safe.
    """

    def __init__(
        self,
        tracker: ContextWindowUtilizationTracker,
        compress_threshold_pct: float = 85.0,
    ):
        self._tracker = tracker
        self._threshold = compress_threshold_pct
        self._callbacks: List[Tuple[int, str, Callable]] = []
        # (priority, name, callback(report) -> bool) — True means "I reduced tokens"

    def register_compression_callback(
        self, name: str, callback: Callable, priority: int = 50
    ) -> None:
        self._callbacks.append((priority, name, callback))
        self._callbacks.sort(key=lambda x: x[0])

    def guard(
        self,
        system_prompt: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
        tool_results: Optional[List[str]] = None,
        injected_documents: Optional[List[str]] = None,
    ) -> ContextUtilizationReport:
        report = self._tracker.measure(
            system_prompt=system_prompt,
            messages=messages,
            tool_results=tool_results,
            injected_documents=injected_documents,
        )

        if report.utilization_pct >= self._threshold:
            for _, name, callback in self._callbacks:
                reduced = callback(report)
                if reduced:
                    break   # re-measure on next call; don't re-measure here

        return report
```

## Solution 6: Context Utilization Dashboard

```python
import time


class ContextWindowUtilizationDashboard:
    """
    Combines utilization history, component growth analysis, and
    over-limit event counts into a single operational view.
    """

    def __init__(
        self,
        tracker: ContextWindowUtilizationTracker,
        growth_detector: PerComponentGrowthDetector,
    ):
        self._tracker = tracker
        self._growth = growth_detector

    def render(self) -> dict:
        budget = self._tracker._budget
        return {
            "generated_at": time.time(),
            "model_context_limit": budget.model_context_limit,
            "output_reserve": budget.output_reserve,
            "available_for_input": budget.available_for_input,
            "utilization_1h": self._tracker.history_summary(3600.0),
            "utilization_24h": self._tracker.history_summary(86400.0),
            "component_growth": self._growth.growth_report(),
        }
```

## Comparison

| Approach | Token Estimation | Component Breakdown | History Tracking | Growth Detection | Overflow Prevention |
|---|---|---|---|---|---|
| TokenEstimator | Yes (tiktoken or ratio) | No | No | No | No |
| ContextWindowUtilizationTracker | Via estimator | Yes | Yes (rolling) | No | No |
| PerComponentGrowthDetector | No | Via tracker | Via snapshots | Yes | No |
| PreemptiveOverflowGuard | Via tracker | Via tracker | Via tracker | No | Yes (callback chain) |
| ContextWindowUtilizationDashboard | No | No | No | No | No |

**Best for production**: Install `tiktoken` and configure the estimator with the exact model being called — character-ratio estimates can be off by 20-30% for non-English text or code-heavy prompts. Set `warn_threshold_pct=80` and `alert_threshold_pct=95`: at 80% emit a structured log event so dashboards trend toward the limit before it becomes a hard failure. Register compression callbacks in order: first compress injected documents (drop lowest-relevance chunks), then summarize conversation history (replace old turns with bullet summaries), and only as a last resort truncate tool results. Monitor `history_summary.over_limit_count` per deployment: any non-zero value means the agent is discovering context overflow at API call time rather than preventing it, which indicates thresholds or compression callbacks need tuning.
