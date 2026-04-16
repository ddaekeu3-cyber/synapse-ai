---
title: "Agent Doesn't Implement Context Window Usage Forecasting"
description: "Agents that don't track context window consumption until it is full are forced to handle context overflow as an error at runtime — truncating mid-conversation, losing tool results, or failing entirely. Implement context window usage forecasting that estimates token consumption at each step, projects when the window will be exhausted given the current trajectory, and triggers proactive summarization or compaction before overflow."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-context-window-usage-forecasting
tags: [context-window, token-forecasting, context-overflow, proactive-summarization, window-management, token-budget]
symptoms:
  - "Agent hits context limit mid-conversation and fails with a token overflow error"
  - "Tool results are silently truncated because the context window filled unexpectedly"
  - "No warning before the context limit is reached — only a hard failure"
  - "Long conversations degrade because there is no budget for new information"
  - "Cannot predict how many more turns are possible before overflow"
---

## Why This Happens

Context window limits are a hard constraint, but most agents only discover they've exceeded the limit when the API returns a 400 error. By then, the conversation state may be corrupted — some messages truncated, some tool results missing. Forecasting requires tracking token consumption at each turn, modeling the growth rate (tokens added per turn), and projecting the number of turns remaining. When the forecast drops below a threshold, the agent can proactively summarize earlier turns or compact tool results, preserving space for future content.

## Solution 1: Token Usage Snapshot

```python
import time
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class TurnTokenSnapshot:
    turn_index: int
    timestamp: float
    system_tokens: int
    conversation_tokens: int
    tool_result_tokens: int
    total_tokens: int
    context_limit: int
    tokens_remaining: int
    utilization: float   # total / limit

    @classmethod
    def create(
        cls,
        turn_index: int,
        system_tokens: int,
        conversation_tokens: int,
        tool_result_tokens: int,
        context_limit: int,
    ) -> "TurnTokenSnapshot":
        total = system_tokens + conversation_tokens + tool_result_tokens
        remaining = max(0, context_limit - total)
        return cls(
            turn_index=turn_index,
            timestamp=time.time(),
            system_tokens=system_tokens,
            conversation_tokens=conversation_tokens,
            tool_result_tokens=tool_result_tokens,
            total_tokens=total,
            context_limit=context_limit,
            tokens_remaining=remaining,
            utilization=round(total / max(context_limit, 1), 4),
        )
```

## Solution 2: Context Usage Tracker

```python
from collections import deque
from threading import Lock
from typing import Deque, List, Optional


class ContextUsageTracker:
    """
    Records per-turn token snapshots and computes growth rate
    for forecasting purposes.
    """

    def __init__(self, context_limit: int, history_size: int = 50):
        self._limit = context_limit
        self._snapshots: Deque[TurnTokenSnapshot] = deque(maxlen=history_size)
        self._lock = Lock()

    def record(self, snapshot: TurnTokenSnapshot) -> None:
        with self._lock:
            self._snapshots.append(snapshot)

    def latest(self) -> Optional[TurnTokenSnapshot]:
        with self._lock:
            return self._snapshots[-1] if self._snapshots else None

    def tokens_added_per_turn(self, lookback: int = 5) -> float:
        """Average tokens added per turn over the last N turns."""
        with self._lock:
            snaps = list(self._snapshots)
        if len(snaps) < 2:
            return 0.0
        recent = snaps[-min(lookback + 1, len(snaps)):]
        deltas = [
            recent[i].total_tokens - recent[i - 1].total_tokens
            for i in range(1, len(recent))
            if recent[i].total_tokens > recent[i - 1].total_tokens
        ]
        if not deltas:
            return 0.0
        return sum(deltas) / len(deltas)

    def utilization_series(self) -> List[float]:
        with self._lock:
            return [s.utilization for s in self._snapshots]
```

## Solution 3: Context Window Forecaster

```python
from typing import Optional


class ContextWindowForecaster:
    """
    Projects how many turns remain before context overflow and
    whether proactive compaction should be triggered.
    """

    def __init__(
        self,
        tracker: ContextUsageTracker,
        compaction_threshold: float = 0.80,   # trigger at 80% utilization
        safety_margin_turns: int = 3,          # trigger early enough to have room to compact
    ):
        self._tracker = tracker
        self._threshold = compaction_threshold
        self._safety_margin = safety_margin_turns

    def turns_remaining(self) -> Optional[int]:
        """Estimated turns before context overflow at current growth rate."""
        latest = self._tracker.latest()
        if latest is None:
            return None
        growth = self._tracker.tokens_added_per_turn()
        if growth <= 0:
            return None
        remaining_tokens = latest.tokens_remaining
        return max(0, int(remaining_tokens / growth))

    def should_compact_now(self) -> bool:
        """True when we should trigger proactive compaction."""
        latest = self._tracker.latest()
        if latest is None:
            return False
        if latest.utilization >= self._threshold:
            return True
        turns = self.turns_remaining()
        if turns is not None and turns <= self._safety_margin:
            return True
        return False

    def forecast(self) -> dict:
        latest = self._tracker.latest()
        if latest is None:
            return {"status": "no_data"}
        growth = self._tracker.tokens_added_per_turn()
        turns = self.turns_remaining()
        return {
            "current_utilization": latest.utilization,
            "tokens_remaining": latest.tokens_remaining,
            "avg_tokens_per_turn": round(growth, 1),
            "estimated_turns_remaining": turns,
            "should_compact": self.should_compact_now(),
            "compaction_threshold": self._threshold,
        }
```

## Solution 4: Proactive Compaction Trigger

```python
import asyncio
from typing import Any, Callable, Optional


class ProactiveCompactionTrigger:
    """
    Monitors the forecaster and invokes a compaction callback when
    the context window is approaching exhaustion.
    Prevents multiple concurrent compaction calls.
    """

    def __init__(
        self,
        forecaster: ContextWindowForecaster,
        compact_fn: Callable,   # async fn() -> None; performs summarization/compaction
    ):
        self._forecaster = forecaster
        self._compact_fn = compact_fn
        self._compacting = False
        self._compaction_count = 0

    async def check_and_compact(self) -> bool:
        """
        Returns True if compaction was triggered.
        """
        if self._compacting:
            return False
        if not self._forecaster.should_compact_now():
            return False

        self._compacting = True
        try:
            await self._compact_fn()
            self._compaction_count += 1
            return True
        finally:
            self._compacting = False

    def stats(self) -> dict:
        return {
            "compaction_count": self._compaction_count,
            "currently_compacting": self._compacting,
            "forecast": self._forecaster.forecast(),
        }
```

## Solution 5: Token Estimator

```python
import re
from typing import Any, Dict, List, Optional


class TokenEstimator:
    """
    Estimates token counts without calling the API.
    Uses the ~4 chars/token heuristic for English text.
    Override with a real tokenizer (tiktoken, transformers) for accuracy.
    """

    CHARS_PER_TOKEN: float = 3.8

    def estimate(self, text: str) -> int:
        return max(1, int(len(text) / self.CHARS_PER_TOKEN))

    def estimate_messages(self, messages: List[Dict[str, Any]]) -> int:
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += self.estimate(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        total += self.estimate(str(block.get("text", "")))
            # overhead per message
            total += 4
        return total

    def snapshot_from_messages(
        self,
        turn_index: int,
        system_messages: List[dict],
        conversation_messages: List[dict],
        tool_result_messages: List[dict],
        context_limit: int,
    ) -> TurnTokenSnapshot:
        return TurnTokenSnapshot.create(
            turn_index=turn_index,
            system_tokens=self.estimate_messages(system_messages),
            conversation_tokens=self.estimate_messages(conversation_messages),
            tool_result_tokens=self.estimate_messages(tool_result_messages),
            context_limit=context_limit,
        )
```

## Solution 6: Context Window Forecast Dashboard

```python
import time


class ContextWindowForecastDashboard:
    """
    Combines usage history, forecast, and compaction trigger stats.
    """

    def __init__(
        self,
        tracker: ContextUsageTracker,
        forecaster: ContextWindowForecaster,
        trigger: ProactiveCompactionTrigger,
    ):
        self._tracker = tracker
        self._forecaster = forecaster
        self._trigger = trigger

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "forecast": self._forecaster.forecast(),
            "utilization_history": self._tracker.utilization_series()[-10:],
            "compaction_stats": self._trigger.stats(),
        }
```

## Comparison

| Approach | Per-Turn Tracking | Growth Rate | Turns Forecast | Auto-Compact | Dashboard |
|---|---|---|---|---|---|
| ContextUsageTracker | Yes | Yes (sliding avg) | No | No | No |
| ContextWindowForecaster | Via tracker | Via tracker | Yes | No | No |
| ProactiveCompactionTrigger | Via forecaster | No | Via forecaster | Yes | No |
| TokenEstimator | No | No | No | No | No |
| ContextWindowForecastDashboard | No | No | No | No | Yes |

**Best for production**: Call `TokenEstimator.snapshot_from_messages()` after every LLM turn and feed it to the tracker — the estimation overhead is microseconds. Set `compaction_threshold=0.75` (not 0.80) to leave enough headroom for the compaction LLM call itself, which consumes tokens. Wire `ProactiveCompactionTrigger.check_and_compact()` into the main agent loop before each new LLM call — not after, when it may be too late. Use `turns_remaining` in the agent's self-awareness: if it forecasts fewer than 5 turns remaining, the agent should prioritize finalizing its answer over gathering more information.
