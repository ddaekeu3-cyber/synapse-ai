---
title: "Agent Doesn't Implement Context Window Utilization Tracking"
description: "Agents that never measure context window fill level cannot predict when the window will be exhausted, cannot distinguish sessions that fail from context overflow vs. other errors, and cannot optimize prompt structure based on which components consume the most tokens. Implement context window utilization tracking that measures token counts per context component, projects fill rate over the conversation, and alerts when utilization approaches the model's limit."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-context-window-utilization-tracking
tags: [context-window, token-tracking, context-utilization, window-overflow, prompt-optimization, token-budget]
symptoms:
  - "Sessions silently fail when context window fills up — error message is cryptic"
  - "No visibility into what fraction of the context window is used vs. available"
  - "Cannot tell whether system prompt, conversation history, or tool schemas uses the most tokens"
  - "Long conversations degrade in quality as older context is truncated without warning"
  - "No alert when a session is approaching the model's context limit"
---

## Why This Happens

Token counts are computed by the LLM API but rarely surfaced as structured metrics. Most agents track total tokens for cost purposes but not the per-component breakdown within the context window. Without per-component tracking, optimizing prompt size is guesswork. Without fill-rate projection, context overflow is always a surprise. Context window utilization tracking requires counting tokens per component (system prompt, history, tool schemas, retrieved chunks, current turn) and monitoring the trajectory as the conversation grows.

## Solution 1: Context Component Record

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class ContextComponent(str, Enum):
    SYSTEM_PROMPT = "system_prompt"
    CONVERSATION_HISTORY = "conversation_history"
    TOOL_SCHEMAS = "tool_schemas"
    RETRIEVED_CHUNKS = "retrieved_chunks"
    CURRENT_TURN = "current_turn"
    TOOL_RESULTS = "tool_results"
    CUSTOM = "custom"


@dataclass
class ContextComponentRecord:
    component: ContextComponent
    label: str
    token_count: int
    turn_index: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
```

## Solution 2: Context Window Snapshot

```python
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class ContextWindowSnapshot:
    session_id: str
    model_id: str
    context_limit: int
    turn_index: int
    components: List[ContextComponentRecord]
    captured_at: float = field(default_factory=time.time)

    def total_tokens(self) -> int:
        return sum(c.token_count for c in self.components)

    def utilization(self) -> float:
        return self.total_tokens() / max(self.context_limit, 1)

    def tokens_remaining(self) -> int:
        return max(0, self.context_limit - self.total_tokens())

    def by_component(self) -> Dict[str, int]:
        result: Dict[str, int] = {}
        for c in self.components:
            result[c.label] = result.get(c.label, 0) + c.token_count
        return result

    def largest_component(self) -> Optional[str]:
        bc = self.by_component()
        if not bc:
            return None
        return max(bc, key=bc.get)
```

## Solution 3: Context Utilization Tracker

```python
from collections import defaultdict
from typing import Dict, List, Optional


class ContextWindowUtilizationTracker:
    """
    Tracks context window snapshots per session.
    Computes fill rate (tokens per turn) and projects when the window will be exhausted.
    """

    def __init__(self):
        self._snapshots: Dict[str, List[ContextWindowSnapshot]] = defaultdict(list)

    def record(self, snapshot: ContextWindowSnapshot) -> None:
        self._snapshots[snapshot.session_id].append(snapshot)

    def snapshots(self, session_id: str) -> List[ContextWindowSnapshot]:
        return list(self._snapshots.get(session_id, []))

    def fill_rate_tokens_per_turn(self, session_id: str) -> Optional[float]:
        snaps = self._snapshots.get(session_id, [])
        if len(snaps) < 2:
            return None
        first = snaps[0]
        last = snaps[-1]
        turn_delta = last.turn_index - first.turn_index
        token_delta = last.total_tokens() - first.total_tokens()
        if turn_delta == 0:
            return None
        return round(token_delta / turn_delta, 1)

    def turns_until_exhaustion(self, session_id: str) -> Optional[float]:
        snaps = self._snapshots.get(session_id, [])
        if not snaps:
            return None
        last = snaps[-1]
        rate = self.fill_rate_tokens_per_turn(session_id)
        if rate is None or rate <= 0:
            return None
        remaining = last.tokens_remaining()
        return round(remaining / rate, 1)

    def current_utilization(self, session_id: str) -> Optional[float]:
        snaps = self._snapshots.get(session_id, [])
        if not snaps:
            return None
        return snaps[-1].utilization()
```

## Solution 4: Token Counter

```python
import re
from typing import List, Optional


class ApproximateTokenCounter:
    """
    Fast approximate token counter without requiring a tokenizer library.
    Uses the heuristic: 1 token ≈ 4 characters for English text.
    For production, replace with the model's actual tokenizer.
    """

    CHARS_PER_TOKEN = 4.0

    @classmethod
    def count(cls, text: str) -> int:
        if not text:
            return 0
        return max(1, round(len(text) / cls.CHARS_PER_TOKEN))

    @classmethod
    def count_messages(cls, messages: List[dict]) -> int:
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += cls.count(content)
            elif isinstance(content, list):
                for block in content:
                    total += cls.count(str(block.get("text", "")))
            total += 4   # message overhead tokens
        return total

    @classmethod
    def count_tool_schemas(cls, tools: List[dict]) -> int:
        import json
        return cls.count(json.dumps(tools, separators=(",", ":")))
```

## Solution 5: Context Window Alert Manager

```python
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional


@dataclass
class ContextWindowAlert:
    session_id: str
    alert_type: str   # "high_utilization" | "near_exhaustion" | "overflow_risk"
    utilization: float
    tokens_remaining: int
    turns_until_exhaustion: Optional[float]
    message: str
    fired_at: float = field(default_factory=time.time)


class ContextWindowAlertManager:
    """
    Fires alerts when context window utilization crosses thresholds.
    """

    def __init__(
        self,
        tracker: ContextWindowUtilizationTracker,
        warning_utilization: float = 0.70,
        critical_utilization: float = 0.90,
        turns_warning_threshold: float = 5.0,
    ):
        self._tracker = tracker
        self._warning = warning_utilization
        self._critical = critical_utilization
        self._turns_warning = turns_warning_threshold
        self._handlers: List[Callable[[ContextWindowAlert], None]] = []

    def add_handler(self, fn: Callable[[ContextWindowAlert], None]) -> None:
        self._handlers.append(fn)

    def evaluate(self, session_id: str) -> List[ContextWindowAlert]:
        util = self._tracker.current_utilization(session_id)
        if util is None:
            return []

        snaps = self._tracker.snapshots(session_id)
        if not snaps:
            return []
        last_snap = snaps[-1]
        turns_left = self._tracker.turns_until_exhaustion(session_id)
        alerts = []

        if util >= self._critical:
            alert = ContextWindowAlert(
                session_id=session_id,
                alert_type="near_exhaustion",
                utilization=round(util, 4),
                tokens_remaining=last_snap.tokens_remaining(),
                turns_until_exhaustion=turns_left,
                message=(
                    f"Context window {util*100:.1f}% full "
                    f"({last_snap.tokens_remaining()} tokens remaining)"
                ),
            )
            alerts.append(alert)
        elif util >= self._warning:
            alert = ContextWindowAlert(
                session_id=session_id,
                alert_type="high_utilization",
                utilization=round(util, 4),
                tokens_remaining=last_snap.tokens_remaining(),
                turns_until_exhaustion=turns_left,
                message=f"Context window {util*100:.1f}% full — consider pruning history",
            )
            alerts.append(alert)

        if turns_left is not None and turns_left <= self._turns_warning and util < self._critical:
            alert = ContextWindowAlert(
                session_id=session_id,
                alert_type="overflow_risk",
                utilization=round(util, 4),
                tokens_remaining=last_snap.tokens_remaining(),
                turns_until_exhaustion=turns_left,
                message=f"At current fill rate, context will exhaust in ~{turns_left:.0f} turns",
            )
            alerts.append(alert)

        for alert in alerts:
            for h in self._handlers:
                try:
                    h(alert)
                except Exception:
                    pass

        return alerts
```

## Solution 6: Context Utilization Dashboard

```python
import time
from typing import List


class ContextWindowUtilizationDashboard:
    """Aggregates context utilization across all active sessions."""

    def __init__(
        self,
        tracker: ContextWindowUtilizationTracker,
        alert_manager: ContextWindowAlertManager,
    ):
        self._tracker = tracker
        self._alert_manager = alert_manager

    def render(self, session_ids: List[str]) -> dict:
        session_reports = []
        for sid in session_ids:
            util = self._tracker.current_utilization(sid)
            snaps = self._tracker.snapshots(sid)
            if not snaps or util is None:
                continue
            last = snaps[-1]
            session_reports.append({
                "session_id": sid,
                "utilization": round(util, 4),
                "total_tokens": last.total_tokens(),
                "tokens_remaining": last.tokens_remaining(),
                "turns_until_exhaustion": self._tracker.turns_until_exhaustion(sid),
                "largest_component": last.largest_component(),
                "breakdown": last.by_component(),
            })

        session_reports.sort(key=lambda x: -x["utilization"])
        high_util = [s for s in session_reports if s["utilization"] >= 0.70]

        return {
            "generated_at": time.time(),
            "sessions_tracked": len(session_reports),
            "high_utilization_sessions": len(high_util),
            "avg_utilization": round(
                sum(s["utilization"] for s in session_reports)
                / max(len(session_reports), 1),
                4,
            ),
            "top_sessions_by_utilization": session_reports[:5],
        }
```

## Comparison

| Approach | Per-Component Tracking | Fill Rate Projection | Exhaustion Forecast | Alerts | Dashboard |
|---|---|---|---|---|---|
| ContextWindowSnapshot | Yes (per-component) | No | No | No | No |
| ContextWindowUtilizationTracker | Via snapshots | Yes (per-turn rate) | Yes (turns left) | No | No |
| ApproximateTokenCounter | No | No | No | No | No |
| ContextWindowAlertManager | Via tracker | Via tracker | Via tracker | Yes | No |
| ContextWindowUtilizationDashboard | Via tracker | No | Via tracker | Via manager | Yes |

**Best for production**: Snapshot the context window after every turn — this is cheap and provides the fill-rate data needed for projection. Track at minimum four components: `system_prompt` (fixed cost), `conversation_history` (grows per turn), `tool_schemas` (often the largest fixed cost), and `retrieved_chunks` (variable per turn). The `largest_component` field typically reveals the optimization target: if `tool_schemas` accounts for 30% of the window, switch to lazy tool loading; if `conversation_history` is 60%, implement history pruning. Alert at 70% utilization to give the pruning system time to act before the window fills.
