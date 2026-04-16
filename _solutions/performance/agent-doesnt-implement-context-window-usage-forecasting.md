---
title: "Agent Doesn't Implement Context Window Usage Forecasting"
description: "Agents that add tool results and conversation history to context without forecasting remaining capacity hit the context limit mid-execution: the agent begins assembling context for an 8-step plan and fails on step 6 when the window fills, having already consumed tokens on steps 1-5. Implement context window usage forecasting that estimates token consumption before each step and decides early whether to truncate, summarize, or abort rather than discovering the limit at execution time."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-context-window-usage-forecasting
tags: [context-window, token-forecasting, capacity-planning, context-overflow-prevention, adaptive-truncation, token-budget]
symptoms:
  - "Agent fails mid-execution when context window is exhausted after 6 of 8 planned steps"
  - "No estimate of how many tokens each planned step will consume"
  - "Context overflow discovered at LLM call time — too late to recover gracefully"
  - "Long conversation histories fill the window before tool results can be added"
  - "No signal that context pressure is building until the hard limit is hit"
---

## Why This Happens

Context window limits are discovered at call time when the tokenized prompt exceeds the model's limit. Agents that build context incrementally — adding conversation history, then tool results, then instructions — have no mechanism to predict whether the next addition will exceed the limit. Forecasting requires maintaining a running token estimate for all committed context, estimating the cost of each planned addition before committing it, and taking a corrective action (truncate history, summarize, skip optional content) when the forecast shows the limit will be exceeded.

## Solution 1: Context Usage Snapshot

```python
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class ContextUsageSnapshot:
    model_context_limit: int
    committed_tokens: int
    reserved_tokens: int          # reserved for response generation
    segments: Dict[str, int] = field(default_factory=dict)
    # segment_name → token_count

    @property
    def available_tokens(self) -> int:
        return max(0, self.model_context_limit - self.committed_tokens - self.reserved_tokens)

    @property
    def utilization(self) -> float:
        return round(self.committed_tokens / max(self.model_context_limit, 1), 4)

    @property
    def pressure(self) -> str:
        u = self.utilization
        if u >= 0.95:
            return "critical"
        if u >= 0.80:
            return "high"
        if u >= 0.60:
            return "medium"
        return "low"
```

## Solution 2: Token Estimator

```python
from typing import Any


class TokenEstimator:
    """
    Estimates token count for text strings and structured data.
    Uses character-based approximation; replace with a real tokenizer
    (e.g. tiktoken) for production accuracy.
    """

    def __init__(self, tokens_per_char: float = 0.25, overhead_per_message: int = 4):
        self._tpc = tokens_per_char
        self._overhead = overhead_per_message

    def estimate(self, content: Any) -> int:
        if content is None:
            return 0
        if isinstance(content, str):
            return max(1, int(len(content) * self._tpc)) + self._overhead
        if isinstance(content, (dict, list)):
            import json
            return self.estimate(json.dumps(content, ensure_ascii=False))
        return self.estimate(str(content))

    def estimate_messages(self, messages: list) -> int:
        return sum(self.estimate(m.get("content", "")) for m in messages)
```

## Solution 3: Context Window Forecaster

```python
from typing import List, Tuple


class ContextWindowForecaster:
    """
    Tracks committed context segments and forecasts whether planned
    additions will fit within the model's context limit.
    """

    def __init__(
        self,
        model_context_limit: int,
        reserved_for_response: int = 2048,
        estimator: TokenEstimator = None,
    ):
        self._limit = model_context_limit
        self._reserved = reserved_for_response
        self._estimator = estimator or TokenEstimator()
        self._segments: dict = {}   # name → token_count
        self._committed = 0

    def commit(self, segment_name: str, content: any, token_count: int = None) -> int:
        tokens = token_count or self._estimator.estimate(content)
        self._segments[segment_name] = tokens
        self._committed = sum(self._segments.values())
        return tokens

    def remove(self, segment_name: str) -> None:
        self._segments.pop(segment_name, None)
        self._committed = sum(self._segments.values())

    def snapshot(self) -> ContextUsageSnapshot:
        return ContextUsageSnapshot(
            model_context_limit=self._limit,
            committed_tokens=self._committed,
            reserved_tokens=self._reserved,
            segments=dict(self._segments),
        )

    def forecast(self, planned_additions: List[Tuple[str, any]]) -> dict:
        """
        Returns a forecast of whether planned_additions will fit.
        planned_additions: list of (name, content) to be added.
        """
        snapshot = self.snapshot()
        planned_tokens = sum(
            self._estimator.estimate(content) for _, content in planned_additions
        )
        post_commit = self._committed + planned_tokens
        will_fit = post_commit + self._reserved <= self._limit
        overflow = max(0, post_commit + self._reserved - self._limit)

        return {
            "will_fit": will_fit,
            "committed_tokens": self._committed,
            "planned_tokens": planned_tokens,
            "post_commit_tokens": post_commit,
            "overflow_tokens": overflow,
            "available_tokens": snapshot.available_tokens,
            "pressure": snapshot.pressure,
        }

    def largest_segments(self, n: int = 5) -> List[Tuple[str, int]]:
        return sorted(self._segments.items(), key=lambda x: -x[1])[:n]
```

## Solution 4: Adaptive Context Trimmer

```python
from typing import List, Optional, Tuple


class AdaptiveContextTrimmer:
    """
    Trims context segments to recover tokens when the forecaster
    predicts an overflow. Trims in priority order: oldest history first,
    then optional enrichment, then tool results.
    """

    def __init__(
        self,
        forecaster: ContextWindowForecaster,
        estimator: TokenEstimator,
    ):
        self._forecaster = forecaster
        self._estimator = estimator

    def trim_to_fit(
        self,
        need_tokens: int,
        trimmable_segments: List[Tuple[str, str, int]],
        # (segment_name, priority, max_trim_tokens)
    ) -> List[dict]:
        """
        Removes or shrinks segments to free at least need_tokens.
        Returns list of trim actions taken.
        """
        freed = 0
        actions = []
        sorted_segs = sorted(trimmable_segments, key=lambda x: x[2], reverse=True)

        for name, priority, max_trim in sorted_segs:
            if freed >= need_tokens:
                break
            current = self._forecaster._segments.get(name, 0)
            if current == 0:
                continue
            trim_amount = min(current, max_trim, need_tokens - freed)
            new_size = current - trim_amount
            if new_size == 0:
                self._forecaster.remove(name)
                actions.append({"action": "remove", "segment": name, "freed": current})
            else:
                self._forecaster._segments[name] = new_size
                self._forecaster._committed = sum(self._forecaster._segments.values())
                actions.append({"action": "trim", "segment": name, "freed": trim_amount, "remaining": new_size})
            freed += trim_amount

        return actions
```

## Solution 5: Forecast-Aware Context Builder

```python
from typing import Any, List, Optional, Tuple


class ForecastAwareContextBuilder:
    """
    Builds context incrementally, checking the forecast before each
    addition and triggering trimming when overflow is predicted.
    """

    def __init__(
        self,
        forecaster: ContextWindowForecaster,
        trimmer: AdaptiveContextTrimmer,
        trimmable_segments: List[Tuple[str, str, int]] = None,
    ):
        self._forecaster = forecaster
        self._trimmer = trimmer
        self._trimmable = trimmable_segments or []
        self._trim_actions: list = []

    def add(
        self,
        segment_name: str,
        content: Any,
        token_count: int = None,
        required: bool = True,
    ) -> dict:
        forecast = self._forecaster.forecast([(segment_name, content)])

        if not forecast["will_fit"]:
            overflow = forecast["overflow_tokens"]
            if self._trimmable:
                trim_actions = self._trimmer.trim_to_fit(overflow, self._trimmable)
                self._trim_actions.extend(trim_actions)
                forecast = self._forecaster.forecast([(segment_name, content)])

            if not forecast["will_fit"] and not required:
                return {"added": False, "segment": segment_name, "reason": "skipped_optional"}

            if not forecast["will_fit"] and required:
                return {"added": False, "segment": segment_name, "reason": "overflow_required"}

        tokens = self._forecaster.commit(segment_name, content, token_count)
        return {
            "added": True,
            "segment": segment_name,
            "tokens": tokens,
            "snapshot": self._forecaster.snapshot().__dict__,
        }

    def trim_history(self) -> List[dict]:
        return list(self._trim_actions)
```

## Solution 6: Context Window Forecast Dashboard

```python
import time


class ContextWindowForecastDashboard:
    """
    Renders the current context window state with pressure indicators
    and largest segment breakdown.
    """

    def __init__(self, forecaster: ContextWindowForecaster):
        self._forecaster = forecaster

    def render(self) -> dict:
        snapshot = self._forecaster.snapshot()
        return {
            "generated_at": time.time(),
            "model_context_limit": snapshot.model_context_limit,
            "committed_tokens": snapshot.committed_tokens,
            "available_tokens": snapshot.available_tokens,
            "utilization_pct": round(snapshot.utilization * 100, 1),
            "pressure": snapshot.pressure,
            "largest_segments": self._forecaster.largest_segments(5),
            "reserved_for_response": snapshot.reserved_tokens,
        }
```

## Comparison

| Approach | Token Estimation | Pre-Addition Forecast | Adaptive Trimming | Segment Tracking | Dashboard |
|---|---|---|---|---|---|
| TokenEstimator | Yes | No | No | No | No |
| ContextWindowForecaster | Via estimator | Yes | No | Yes | No |
| AdaptiveContextTrimmer | Via estimator | Via forecaster | Yes | Via forecaster | No |
| ForecastAwareContextBuilder | Via forecaster | Yes | Via trimmer | Via forecaster | No |
| ContextWindowForecastDashboard | No | No | No | Via forecaster | Yes |

**Best for production**: Replace `TokenEstimator` with `tiktoken` for the exact model being used — character-based approximations can be off by 30% for non-English text and code, causing false "will fit" forecasts. Set `reserved_for_response=2048` for chat models and `4096` for long-form generation models. Declare conversation history as the first trimmable segment with the highest `max_trim_tokens` — it is the largest and most safely reducible. Alert when `pressure == "critical"` (>95% utilization) before the LLM call rather than after: a critical-pressure context that adds even a small tool result will overflow.
