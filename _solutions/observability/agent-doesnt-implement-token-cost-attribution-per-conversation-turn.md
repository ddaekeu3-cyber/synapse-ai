---
title: "Agent Doesn't Implement Token Cost Attribution Per Conversation Turn"
description: "Agents that track only total session token usage cannot explain which turns, tools, or user actions drove the highest costs. A single complex turn that retrieves five documents and makes three LLM calls may account for 80% of session cost, but without turn-level attribution this is invisible. Implement token cost attribution that records input tokens, output tokens, and cache metrics per turn, per tool result injection, and per LLM call, enabling cost drill-down from session to turn to individual operation."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-token-cost-attribution-per-conversation-turn
tags: [token-cost-attribution, per-turn-cost, cost-breakdown, llm-cost-tracking, context-cost, billing-observability]
symptoms:
  - "Total session cost is known but no breakdown by turn or tool result is available"
  - "High-cost sessions cannot be explained — only the aggregate token count is visible"
  - "Cannot identify which prompt patterns or tool combinations drive the most spend"
  - "Cost per user request is estimated from averages, not measured per request"
  - "No feedback loop from token cost to prompt engineering decisions"
---

## Why This Happens

LLM provider APIs return token usage in the response, but agents accumulate these numbers in session-level counters without attaching them to the turn that generated the cost. Once the session counter is incremented, the per-turn signal is gone. Turn-level attribution requires capturing the usage metadata from each API response and storing it alongside the turn identifier, tool calls made, and the context size at that point. Without this structure, cost optimization is guesswork.

## Solution 1: Token Usage Record

```python
from dataclasses import dataclass, field
from typing import Optional
import time


@dataclass
class TokenUsageRecord:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def billable_input_tokens(self) -> int:
        """Input tokens minus those served from cache (already paid for)."""
        return max(0, self.input_tokens - self.cache_read_tokens)

    def cost_usd(
        self,
        input_cost_per_m: float = 3.0,
        output_cost_per_m: float = 15.0,
        cache_read_cost_per_m: float = 0.30,
        cache_write_cost_per_m: float = 3.75,
    ) -> float:
        return round(
            self.billable_input_tokens / 1_000_000 * input_cost_per_m
            + self.output_tokens / 1_000_000 * output_cost_per_m
            + self.cache_read_tokens / 1_000_000 * cache_read_cost_per_m
            + self.cache_creation_tokens / 1_000_000 * cache_write_cost_per_m,
            8,
        )

    def __add__(self, other: "TokenUsageRecord") -> "TokenUsageRecord":
        return TokenUsageRecord(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_creation_tokens=self.cache_creation_tokens + other.cache_creation_tokens,
        )
```

## Solution 2: Turn Cost Record

```python
from dataclasses import dataclass, field
from typing import Dict, List, Optional
import time


@dataclass
class LLMCallRecord:
    call_index: int
    usage: TokenUsageRecord
    duration_ms: float
    model: str = ""


@dataclass
class TurnCostRecord:
    turn_id: str
    session_id: str
    turn_index: int
    started_at: float = field(default_factory=time.time)
    llm_calls: List[LLMCallRecord] = field(default_factory=list)
    tool_result_tokens: Dict[str, int] = field(default_factory=dict)
    # tool_name -> estimated tokens injected into context

    def total_usage(self) -> TokenUsageRecord:
        total = TokenUsageRecord()
        for call in self.llm_calls:
            total = total + call.usage
        return total

    def total_cost_usd(self) -> float:
        return self.total_usage().cost_usd()

    def llm_call_count(self) -> int:
        return len(self.llm_calls)
```

## Solution 3: Turn Cost Tracker

```python
import time
from typing import Any, Dict, List, Optional


class TurnCostTracker:
    """
    Records token usage for each LLM call within a turn.
    Parses usage from provider API responses.
    """

    def __init__(self, session_id: str):
        self._session_id = session_id
        self._turns: Dict[str, TurnCostRecord] = {}
        self._current_turn_id: Optional[str] = None
        self._call_counter = 0

    def start_turn(self, turn_id: str, turn_index: int) -> TurnCostRecord:
        record = TurnCostRecord(
            turn_id=turn_id,
            session_id=self._session_id,
            turn_index=turn_index,
        )
        self._turns[turn_id] = record
        self._current_turn_id = turn_id
        self._call_counter = 0
        return record

    def record_llm_call(
        self,
        response_usage: Any,
        duration_ms: float,
        model: str = "",
        turn_id: Optional[str] = None,
    ) -> None:
        tid = turn_id or self._current_turn_id
        if tid is None or tid not in self._turns:
            return

        usage = TokenUsageRecord(
            input_tokens=getattr(response_usage, "input_tokens", 0) or 0,
            output_tokens=getattr(response_usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(response_usage, "cache_read_input_tokens", 0) or 0,
            cache_creation_tokens=getattr(response_usage, "cache_creation_input_tokens", 0) or 0,
        )
        self._call_counter += 1
        self._turns[tid].llm_calls.append(LLMCallRecord(
            call_index=self._call_counter,
            usage=usage,
            duration_ms=duration_ms,
            model=model,
        ))

    def record_tool_result_tokens(
        self,
        tool_name: str,
        token_estimate: int,
        turn_id: Optional[str] = None,
    ) -> None:
        tid = turn_id or self._current_turn_id
        if tid and tid in self._turns:
            existing = self._turns[tid].tool_result_tokens.get(tool_name, 0)
            self._turns[tid].tool_result_tokens[tool_name] = existing + token_estimate

    def turn_record(self, turn_id: str) -> Optional[TurnCostRecord]:
        return self._turns.get(turn_id)

    def all_turns(self) -> List[TurnCostRecord]:
        return list(self._turns.values())
```

## Solution 4: Session Cost Aggregator

```python
from typing import Dict, List, Optional


class SessionCostAggregator:
    """
    Aggregates turn-level cost records into session totals and
    identifies the highest-cost turns for investigation.
    """

    def summarize(self, tracker: TurnCostTracker) -> dict:
        turns = tracker.all_turns()
        if not turns:
            return {"turns": 0, "total_cost_usd": 0.0}

        turn_costs = [
            {
                "turn_id": t.turn_id,
                "turn_index": t.turn_index,
                "llm_calls": t.llm_call_count(),
                "input_tokens": t.total_usage().input_tokens,
                "output_tokens": t.total_usage().output_tokens,
                "cache_read_tokens": t.total_usage().cache_read_tokens,
                "tool_result_tokens": sum(t.tool_result_tokens.values()),
                "cost_usd": round(t.total_cost_usd(), 8),
            }
            for t in turns
        ]

        total = TokenUsageRecord()
        for turn in turns:
            total = total + turn.total_usage()

        return {
            "session_id": tracker._session_id,
            "turns": len(turns),
            "total_input_tokens": total.input_tokens,
            "total_output_tokens": total.output_tokens,
            "total_cache_read_tokens": total.cache_read_tokens,
            "total_cost_usd": round(total.cost_usd(), 8),
            "avg_cost_per_turn_usd": round(total.cost_usd() / len(turns), 8),
            "top_cost_turns": sorted(turn_costs, key=lambda x: -x["cost_usd"])[:3],
            "per_turn": turn_costs,
        }
```

## Solution 5: Fleet Cost Reporter

```python
import time
from collections import deque
from threading import Lock
from typing import Deque


class FleetTokenCostReporter:
    """
    Accumulates session cost summaries and provides fleet-level
    cost percentile and trend data for budgeting and alerting.
    """

    def __init__(self, max_sessions: int = 50_000, window_seconds: float = 86400.0):
        self._max = max_sessions
        self._window = window_seconds
        self._records: Deque[dict] = deque()
        self._lock = Lock()

    def record_session(self, summary: dict) -> None:
        with self._lock:
            self._records.append({**summary, "recorded_at": time.time()})
            if len(self._records) > self._max:
                self._records.popleft()

    def fleet_summary(self, window_seconds: Optional[float] = None) -> dict:
        win = window_seconds or self._window
        cutoff = time.time() - win
        with self._lock:
            recent = [r for r in self._records if r.get("recorded_at", 0) >= cutoff]

        if not recent:
            return {"sessions": 0}

        costs = sorted(r.get("total_cost_usd", 0.0) for r in recent)

        def pct(p: float) -> float:
            idx = min(int(len(costs) * p), len(costs) - 1)
            return round(costs[idx], 8)

        return {
            "window_seconds": win,
            "sessions": len(recent),
            "total_cost_usd": round(sum(costs), 4),
            "cost_p50_usd": pct(0.50),
            "cost_p95_usd": pct(0.95),
            "cost_p99_usd": pct(0.99),
            "avg_cost_usd": round(sum(costs) / len(costs), 8),
        }
```

## Solution 6: Token Cost Attribution Dashboard

```python
import time
from typing import Optional


class TokenCostAttributionDashboard:
    """
    Renders per-session cost breakdown, top expensive turns,
    and fleet cost distribution for billing and prompt optimization.
    """

    def __init__(
        self,
        aggregator: SessionCostAggregator,
        fleet_reporter: FleetTokenCostReporter,
    ):
        self._aggregator = aggregator
        self._fleet = fleet_reporter

    def render_session(self, tracker: TurnCostTracker) -> dict:
        summary = self._aggregator.summarize(tracker)
        self._fleet.record_session(summary)
        return {
            "generated_at": time.time(),
            "session_summary": summary,
            "fleet_1h": self._fleet.fleet_summary(3600.0),
        }
```

## Comparison

| Approach | Per-LLM-Call Tracking | Per-Turn Rollup | Tool Result Attribution | Session Aggregation | Fleet Reporting |
|---|---|---|---|---|---|
| TurnCostTracker | Yes | Yes | Yes (estimate) | No | No |
| SessionCostAggregator | Via tracker | Via tracker | Via tracker | Yes | No |
| FleetTokenCostReporter | No | No | No | Via session | Yes |
| TokenCostAttributionDashboard | No | No | No | Via aggregator | Via reporter |

**Best for production**: Call `record_llm_call()` immediately after every provider API response — do not batch or defer; the usage object may not be available later. Estimate `tool_result_tokens` at 0.25 tokens/char for unstructured text and 0.30 tokens/char for JSON — these are rough but sufficient for cost attribution. Use `top_cost_turns` from `SessionCostAggregator.summarize()` to identify which turn patterns are most expensive: a single turn with 5 tool calls and a 4,000-token context injection is a refactoring candidate. Set a per-session cost budget alert at 3× `cost_p95_usd` from the fleet reporter — sessions above this are outliers worth investigating.
