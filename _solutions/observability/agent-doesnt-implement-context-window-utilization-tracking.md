---
title: "Agent Doesn't Implement Context Window Utilization Tracking"
description: "Agents that don't track context window fill rate operate blindly until they hit a context overflow error — no warning before truncation, no visibility into which sessions are consuming the most context, and no signal to trigger proactive summarization before the window fills. Implement context window utilization tracking that measures token fill percentage per turn, projects turns-to-overflow at the current growth rate, and alerts when utilization crosses warning thresholds."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-context-window-utilization-tracking
tags: [context-window, token-utilization, overflow-prevention, context-management, turn-tracking, context-observability]
symptoms:
  - "Agent crashes with context length exceeded error with no prior warning"
  - "No per-session metric for how full the context window is at each turn"
  - "Cannot predict which sessions will overflow in the next 3 turns"
  - "Truncation happens silently — oldest messages disappear with no log entry"
  - "No signal to trigger proactive summarization before hitting the hard limit"
---

## Why This Happens

Context window overflow is a predictable, gradual failure — the window fills token by token across turns. Without turn-by-turn fill rate measurement, the agent has no early warning. The first signal is either a provider error (hard overflow) or silent truncation (missing context). Utilization tracking transforms this from a surprise failure into a monitored metric: track tokens per turn, compute fill percentage, project overflow timing, and trigger summarization or truncation before the limit is reached.

## Solution 1: Context Window Snapshot

```python
import time
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class ContextWindowSnapshot:
    session_id: str
    turn_number: int
    model: str
    context_limit_tokens: int
    used_tokens: int
    system_tokens: int
    history_tokens: int
    tool_result_tokens: int
    recorded_at: float = field(default_factory=time.time)

    def utilization_pct(self) -> float:
        return round(self.used_tokens / max(self.context_limit_tokens, 1) * 100, 2)

    def tokens_remaining(self) -> int:
        return max(0, self.context_limit_tokens - self.used_tokens)

    def is_critical(self, critical_threshold: float = 90.0) -> bool:
        return self.utilization_pct() >= critical_threshold

    def is_warning(self, warning_threshold: float = 70.0) -> bool:
        return self.utilization_pct() >= warning_threshold
```

## Solution 2: Per-Model Context Limit Registry

```python
from typing import Dict, Optional


MODEL_CONTEXT_LIMITS: Dict[str, int] = {
    # Anthropic
    "claude-opus-4-6": 200_000,
    "claude-sonnet-4-6": 200_000,
    "claude-haiku-4-5-20251001": 200_000,
    "claude-3-5-sonnet-20241022": 200_000,
    "claude-3-5-haiku-20241022": 200_000,
    # OpenAI
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-3.5-turbo": 16_385,
    # Google
    "gemini-1.5-pro": 1_000_000,
    "gemini-1.5-flash": 1_000_000,
}


class ModelContextLimitRegistry:
    """
    Returns the context window size for a given model.
    Falls back to a conservative default for unknown models.
    """

    DEFAULT_LIMIT = 8_192

    def __init__(self, overrides: Optional[Dict[str, int]] = None) -> None:
        self._limits = dict(MODEL_CONTEXT_LIMITS)
        if overrides:
            self._limits.update(overrides)

    def get(self, model: str) -> int:
        return self._limits.get(model, self.DEFAULT_LIMIT)

    def register(self, model: str, limit: int) -> None:
        self._limits[model] = limit

    def all_limits(self) -> Dict[str, int]:
        return dict(self._limits)
```

## Solution 3: Context Utilization Tracker

```python
from collections import defaultdict, deque
from typing import Dict, List, Optional


class ContextUtilizationTracker:
    """
    Records context window snapshots per session and computes
    growth rate, turns-to-overflow projection, and utilization trends.
    """

    def __init__(
        self,
        limit_registry: ModelContextLimitRegistry,
        max_history_per_session: int = 100,
    ) -> None:
        self._registry = limit_registry
        self._max_history = max_history_per_session
        self._sessions: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=max_history_per_session)
        )

    def record(
        self,
        session_id: str,
        turn_number: int,
        model: str,
        used_tokens: int,
        system_tokens: int = 0,
        history_tokens: int = 0,
        tool_result_tokens: int = 0,
    ) -> ContextWindowSnapshot:
        limit = self._registry.get(model)
        snap = ContextWindowSnapshot(
            session_id=session_id,
            turn_number=turn_number,
            model=model,
            context_limit_tokens=limit,
            used_tokens=used_tokens,
            system_tokens=system_tokens,
            history_tokens=history_tokens,
            tool_result_tokens=tool_result_tokens,
        )
        self._sessions[session_id].append(snap)
        return snap

    def latest(self, session_id: str) -> Optional[ContextWindowSnapshot]:
        history = self._sessions.get(session_id)
        if not history:
            return None
        return history[-1]

    def growth_rate_tokens_per_turn(self, session_id: str, window: int = 5) -> Optional[float]:
        """Average token growth per turn over the last `window` snapshots."""
        history = list(self._sessions.get(session_id, []))
        if len(history) < 2:
            return None
        recent = history[-window:]
        if len(recent) < 2:
            return None
        delta_tokens = recent[-1].used_tokens - recent[0].used_tokens
        delta_turns = recent[-1].turn_number - recent[0].turn_number
        if delta_turns <= 0:
            return None
        return round(delta_tokens / delta_turns, 1)

    def turns_to_overflow(
        self,
        session_id: str,
        warning_pct: float = 95.0,
    ) -> Optional[float]:
        snap = self.latest(session_id)
        if not snap:
            return None
        rate = self.growth_rate_tokens_per_turn(session_id)
        if rate is None or rate <= 0:
            return None
        target = snap.context_limit_tokens * (warning_pct / 100.0)
        remaining = target - snap.used_tokens
        if remaining <= 0:
            return 0.0
        return round(remaining / rate, 1)

    def utilization_trend(self, session_id: str) -> List[dict]:
        return [
            {
                "turn": s.turn_number,
                "used_tokens": s.used_tokens,
                "utilization_pct": s.utilization_pct(),
            }
            for s in self._sessions.get(session_id, [])
        ]
```

## Solution 4: Context Overflow Alert Manager

```python
import time
from typing import Callable, Dict, List, Optional


class ContextOverflowAlertManager:
    """
    Fires alerts when context utilization crosses warning or critical thresholds,
    or when overflow is projected within a configurable number of turns.
    """

    def __init__(
        self,
        tracker: ContextUtilizationTracker,
        warning_pct: float = 70.0,
        critical_pct: float = 90.0,
        tte_warning_turns: float = 5.0,
        handler: Optional[Callable[[dict], None]] = None,
        cooldown_seconds: float = 120.0,
    ) -> None:
        self._tracker = tracker
        self._warning = warning_pct
        self._critical = critical_pct
        self._tte_turns = tte_warning_turns
        self._handler = handler
        self._cooldown = cooldown_seconds
        self._last_fired: Dict[str, float] = {}

    def _can_fire(self, key: str) -> bool:
        last = self._last_fired.get(key, 0.0)
        if time.time() - last >= self._cooldown:
            self._last_fired[key] = time.time()
            return True
        return False

    def check(self, session_id: str) -> List[dict]:
        snap = self._tracker.latest(session_id)
        if not snap:
            return []

        alerts = []
        pct = snap.utilization_pct()
        tte = self._tracker.turns_to_overflow(session_id)

        if pct >= self._critical and self._can_fire(f"{session_id}:critical"):
            alerts.append({
                "type": "context_critical",
                "session_id": session_id,
                "utilization_pct": pct,
                "used_tokens": snap.used_tokens,
                "limit_tokens": snap.context_limit_tokens,
                "severity": "critical",
                "message": f"Session '{session_id}' context at {pct:.1f}% — overflow imminent",
            })
        elif pct >= self._warning and self._can_fire(f"{session_id}:warning"):
            alerts.append({
                "type": "context_warning",
                "session_id": session_id,
                "utilization_pct": pct,
                "severity": "warning",
                "message": f"Session '{session_id}' context at {pct:.1f}%",
            })

        if tte is not None and tte <= self._tte_turns and self._can_fire(f"{session_id}:tte"):
            alerts.append({
                "type": "context_overflow_imminent",
                "session_id": session_id,
                "turns_to_overflow": tte,
                "severity": "warning",
                "message": (
                    f"Session '{session_id}' will overflow in ~{tte:.0f} turns "
                    "at current growth rate — trigger summarization"
                ),
                "suggested_action": "summarize_and_truncate",
            })

        for alert in alerts:
            if self._handler:
                try:
                    self._handler(alert)
                except Exception:
                    pass

        return alerts
```

## Solution 5: Fleet-Wide Utilization Aggregator

```python
import time
from typing import Dict, List


class FleetContextUtilizationAggregator:
    """
    Aggregates context utilization across all active sessions
    to identify fleet-wide trends and high-utilization outliers.
    """

    def __init__(
        self,
        tracker: ContextUtilizationTracker,
        high_utilization_threshold: float = 80.0,
    ) -> None:
        self._tracker = tracker
        self._threshold = high_utilization_threshold

    def aggregate(self, session_ids: List[str]) -> dict:
        snapshots = [
            self._tracker.latest(sid)
            for sid in session_ids
            if self._tracker.latest(sid)
        ]
        if not snapshots:
            return {"sessions": 0}

        utilizations = [s.utilization_pct() for s in snapshots]
        high_util = [s for s in snapshots if s.utilization_pct() >= self._threshold]

        return {
            "sessions": len(snapshots),
            "mean_utilization_pct": round(sum(utilizations) / len(utilizations), 2),
            "max_utilization_pct": round(max(utilizations), 2),
            "high_utilization_sessions": len(high_util),
            "high_utilization_pct": round(len(high_util) / len(snapshots) * 100, 1),
            "at_risk_sessions": [
                {"session_id": s.session_id, "utilization_pct": s.utilization_pct()}
                for s in sorted(high_util, key=lambda x: -x.utilization_pct())[:10]
            ],
        }
```

## Solution 6: Context Window Utilization Dashboard

```python
import time


class ContextWindowUtilizationDashboard:
    """
    Combines per-session snapshots, fleet aggregation, and overflow alerts
    into a single context management observability report.
    """

    def __init__(
        self,
        tracker: ContextUtilizationTracker,
        alert_manager: ContextOverflowAlertManager,
        aggregator: FleetContextUtilizationAggregator,
    ) -> None:
        self._tracker = tracker
        self._alerts = alert_manager
        self._aggregator = aggregator

    def render(self, active_session_ids: List[str]) -> dict:
        fleet = self._aggregator.aggregate(active_session_ids)
        all_alerts = []
        for sid in active_session_ids:
            all_alerts.extend(self._alerts.check(sid))

        critical_alerts = [a for a in all_alerts if a.get("severity") == "critical"]

        return {
            "generated_at": time.time(),
            "fleet": fleet,
            "active_alerts": all_alerts,
            "critical_count": len(critical_alerts),
        }
```

## Comparison

| Approach | Per-Turn Tracking | Growth Rate | Overflow Projection | Fleet View | Alerts |
|---|---|---|---|---|---|
| ContextUtilizationTracker | Yes | Yes (sliding window) | Yes (turns-to-overflow) | No | No |
| ContextOverflowAlertManager | Via tracker | Via tracker | Via tracker | No | Yes (with cooldown) |
| FleetContextUtilizationAggregator | No | No | No | Yes | No |
| ContextWindowUtilizationDashboard | No | No | No | Via aggregator | Via manager |

**Best for production**: Record a `ContextWindowSnapshot` after every LLM response using the token counts from the API response's `usage` field — these are exact, not estimated. Set `warning_pct=70` and `critical_pct=90`: at 70% there is still time for summarization; at 90% the window for graceful intervention is closing. Wire `suggested_action: summarize_and_truncate` alerts to automatically trigger a conversation summarization step rather than waiting for manual intervention. For long-running agents (customer support, research assistants), target keeping context utilization below 60% by summarizing proactively — this reserves headroom for tool results and long model responses.
