---
title: "Agent Doesn't Implement LLM Context Utilization Trending"
description: "Agents that never track how much of the available context window they actually use make inefficient use of expensive token budgets and are blindsided when conversations approach the context limit: history fills the window, tool results push the agent over the limit, and truncation silently degrades quality. Implement context utilization trending that measures utilization per turn, detects fill-rate trends, and alerts before the window is exhausted."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-llm-context-utilization-trending
tags: [context-utilization, context-window, token-trending, fill-rate, utilization-tracking, context-overflow-prevention]
symptoms:
  - "Conversations silently degrade when history fills the context window"
  - "No metric for how full the context window is at each turn"
  - "Agent hits context limit unexpectedly during long sessions"
  - "No signal to trigger history summarization before overflow"
  - "Context budget is allocated by guesswork rather than measured utilization"
---

## Why This Happens

Context window utilization is rarely measured because it requires token counting at every turn, which adds latency. Most agents track token usage only in the API response metadata, which records input tokens consumed but provides no per-component breakdown. Without per-turn utilization measurements, there is no signal to trigger proactive management (summarization, eviction, pagination) before the window is full. Trending requires measuring input tokens per turn, tracking the rate of growth, and projecting when the window will be exhausted given the current fill rate.

## Solution 1: Context Utilization Snapshot

```python
import time
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class ContextUtilizationSnapshot:
    session_id: str
    turn_number: int
    input_tokens: int
    context_window_size: int
    utilization_pct: float
    component_tokens: Dict[str, int] = field(default_factory=dict)
    # e.g. {"history": 800, "system_prompt": 200, "tool_results": 400}
    taken_at: float = field(default_factory=time.time)

    def tokens_remaining(self) -> int:
        return max(0, self.context_window_size - self.input_tokens)

    def is_critical(self, threshold_pct: float = 85.0) -> bool:
        return self.utilization_pct >= threshold_pct
```

## Solution 2: Per-Turn Context Token Counter

```python
from typing import Any, Callable, Dict, List, Optional


class PerTurnContextTokenCounter:
    """
    Counts tokens in each component of the context before an LLM call.
    Uses a provided counting function (e.g., tiktoken) and returns
    a breakdown by component name.
    """

    def __init__(self, count_fn: Callable[[str], int]):
        self._count_fn = count_fn

    def count_components(
        self,
        components: Dict[str, Any],
    ) -> Dict[str, int]:
        """
        components: dict of component_name -> content (str or list of messages).
        Returns dict of component_name -> token count.
        """
        result: Dict[str, int] = {}
        for name, content in components.items():
            if isinstance(content, str):
                result[name] = self._count_fn(content)
            elif isinstance(content, list):
                text = " ".join(
                    m.get("content", "") if isinstance(m, dict) else str(m)
                    for m in content
                )
                result[name] = self._count_fn(text)
            else:
                result[name] = self._count_fn(str(content))
        return result

    def total(self, component_counts: Dict[str, int]) -> int:
        return sum(component_counts.values())
```

## Solution 3: Context Fill Rate Tracker

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Dict, List, Optional, Tuple


class ContextFillRateTracker:
    """
    Tracks per-session context utilization over turns.
    Computes the fill rate (tokens added per turn) and projects
    when the session will exhaust the context window.
    """

    def __init__(self, max_snapshots_per_session: int = 200):
        self._max = max_snapshots_per_session
        self._sessions: Dict[str, Deque[ContextUtilizationSnapshot]] = {}
        self._lock = Lock()

    def record(self, snapshot: ContextUtilizationSnapshot) -> None:
        with self._lock:
            if snapshot.session_id not in self._sessions:
                self._sessions[snapshot.session_id] = deque(maxlen=self._max)
            self._sessions[snapshot.session_id].append(snapshot)

    def fill_rate(self, session_id: str) -> Optional[float]:
        """Returns average tokens added per turn over the session history."""
        with self._lock:
            snaps = list(self._sessions.get(session_id, []))
        if len(snaps) < 2:
            return None
        token_deltas = [
            snaps[i].input_tokens - snaps[i - 1].input_tokens
            for i in range(1, len(snaps))
            if snaps[i].input_tokens > snaps[i - 1].input_tokens
        ]
        if not token_deltas:
            return None
        return sum(token_deltas) / len(token_deltas)

    def turns_until_exhaustion(self, session_id: str) -> Optional[int]:
        with self._lock:
            snaps = list(self._sessions.get(session_id, []))
        if not snaps:
            return None
        latest = snaps[-1]
        rate = self.fill_rate(session_id)
        if not rate or rate <= 0:
            return None
        remaining = latest.tokens_remaining()
        return max(0, int(remaining / rate))

    def projection(self, session_id: str) -> dict:
        snaps_copy = []
        with self._lock:
            snaps_copy = list(self._sessions.get(session_id, []))
        if not snaps_copy:
            return {"session_id": session_id, "status": "no_data"}
        latest = snaps_copy[-1]
        rate = self.fill_rate(session_id)
        turns_left = self.turns_until_exhaustion(session_id)
        return {
            "session_id": session_id,
            "current_turn": latest.turn_number,
            "utilization_pct": latest.utilization_pct,
            "tokens_remaining": latest.tokens_remaining(),
            "fill_rate_per_turn": round(rate, 1) if rate else None,
            "estimated_turns_remaining": turns_left,
            "status": (
                "critical" if latest.is_critical(85) else
                "warning" if latest.is_critical(70) else
                "healthy"
            ),
        }
```

## Solution 4: Context Overflow Alert Manager

```python
import time
from typing import Callable, Dict, List, Optional


class ContextOverflowAlertManager:
    """
    Fires alerts when context utilization crosses configured thresholds
    or when the estimated turns remaining drops below a minimum.
    """

    def __init__(
        self,
        alert_fn: Optional[Callable[[dict], None]] = None,
        utilization_warning_pct: float = 70.0,
        utilization_critical_pct: float = 85.0,
        min_turns_warning: int = 5,
    ):
        self._alert_fn = alert_fn
        self._warning_pct = utilization_warning_pct
        self._critical_pct = utilization_critical_pct
        self._min_turns = min_turns_warning
        self._fired: Dict[str, str] = {}  # session_id -> last alert level
        self._alert_log: List[dict] = []

    def evaluate(
        self,
        projection: dict,
    ) -> Optional[dict]:
        session_id = projection.get("session_id", "")
        utilization = projection.get("utilization_pct", 0.0)
        turns_left = projection.get("estimated_turns_remaining")

        level = None
        reason = ""

        if utilization >= self._critical_pct:
            level = "critical"
            reason = f"utilization {utilization:.1f}% >= critical threshold {self._critical_pct}%"
        elif utilization >= self._warning_pct:
            level = "warning"
            reason = f"utilization {utilization:.1f}% >= warning threshold {self._warning_pct}%"
        elif turns_left is not None and turns_left <= self._min_turns:
            level = "warning"
            reason = f"only {turns_left} turns estimated before context exhaustion"

        if level and self._fired.get(session_id) != level:
            alert = {
                "session_id": session_id,
                "level": level,
                "reason": reason,
                "utilization_pct": utilization,
                "turns_remaining": turns_left,
                "fired_at": time.time(),
            }
            self._fired[session_id] = level
            self._alert_log.append(alert)
            if self._alert_fn:
                self._alert_fn(alert)
            return alert

        if not level:
            self._fired.pop(session_id, None)

        return None

    def recent_alerts(self, limit: int = 20) -> List[dict]:
        return self._alert_log[-limit:]
```

## Solution 5: Instrumented Context Builder

```python
import time
from typing import Any, Dict, List, Optional


class InstrumentedContextBuilder:
    """
    Builds the LLM context while measuring per-component token usage
    and recording a utilization snapshot for trending.
    """

    def __init__(
        self,
        counter: PerTurnContextTokenCounter,
        tracker: ContextFillRateTracker,
        alert_manager: ContextOverflowAlertManager,
        context_window_size: int = 200000,
    ):
        self._counter = counter
        self._tracker = tracker
        self._alerts = alert_manager
        self._window = context_window_size
        self._turn_counters: Dict[str, int] = {}

    def build(
        self,
        session_id: str,
        components: Dict[str, Any],
    ) -> dict:
        self._turn_counters[session_id] = self._turn_counters.get(session_id, 0) + 1
        turn = self._turn_counters[session_id]

        component_counts = self._counter.count_components(components)
        total = self._counter.total(component_counts)
        utilization_pct = round(total / self._window * 100, 2)

        snapshot = ContextUtilizationSnapshot(
            session_id=session_id,
            turn_number=turn,
            input_tokens=total,
            context_window_size=self._window,
            utilization_pct=utilization_pct,
            component_tokens=component_counts,
        )
        self._tracker.record(snapshot)

        projection = self._tracker.projection(session_id)
        alert = self._alerts.evaluate(projection)

        return {
            "snapshot": snapshot,
            "projection": projection,
            "alert": alert,
            "component_tokens": component_counts,
            "total_tokens": total,
            "utilization_pct": utilization_pct,
        }
```

## Solution 6: Context Utilization Dashboard

```python
import time
from typing import List


class ContextUtilizationDashboard:
    """
    Surfaces per-session utilization trends, fill rates, and
    overflow projections in a single operational view.
    """

    def __init__(
        self,
        tracker: ContextFillRateTracker,
        alert_manager: ContextOverflowAlertManager,
    ):
        self._tracker = tracker
        self._alerts = alert_manager

    def render(self, session_ids: List[str]) -> dict:
        projections = [
            self._tracker.projection(sid) for sid in session_ids
        ]
        critical = [p for p in projections if p.get("status") == "critical"]
        warning = [p for p in projections if p.get("status") == "warning"]

        return {
            "generated_at": time.time(),
            "sessions_monitored": len(session_ids),
            "critical_sessions": len(critical),
            "warning_sessions": len(warning),
            "projections": projections,
            "recent_alerts": self._alerts.recent_alerts(limit=10),
        }
```

## Comparison

| Approach | Per-Turn Measurement | Component Breakdown | Fill Rate Trending | Overflow Alert | Dashboard |
|---|---|---|---|---|---|
| PerTurnContextTokenCounter | Yes (per component) | Yes | No | No | No |
| ContextFillRateTracker | No | No | Yes (per-turn delta) | No | No |
| ContextOverflowAlertManager | No | No | No | Yes (threshold) | No |
| InstrumentedContextBuilder | Via counter | Via counter | Via tracker | Via manager | No |
| ContextUtilizationDashboard | No | No | Via tracker | Via manager | Yes |

**Best for production**: Fire the `warning` alert at 70% utilization and trigger automatic history summarization — this leaves 30% of the window available for the summarized history plus new turns. Fire `critical` at 85% and begin aggressive eviction of the oldest history segments. Use `fill_rate_per_turn` to dynamically adjust how many history turns to retain: if the fill rate is 200 tokens/turn and 3000 tokens remain, only 15 more turns fit — proactive summarization should begin immediately. Track `component_tokens` breakdown to identify which component (tool results, history, documents) is consuming disproportionate space and optimize that layer first.
