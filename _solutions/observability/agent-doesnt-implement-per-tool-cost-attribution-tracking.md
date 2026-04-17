---
title: "Agent Doesn't Implement Per-Tool Cost Attribution Tracking"
description: "Agents that track total LLM and API spending without per-tool attribution cannot identify which tools drive the majority of cost: a single expensive search tool making 50 calls per conversation may account for 70% of total spend while being invisible in aggregate billing dashboards. Implement per-tool cost attribution that tracks token consumption, API call cost, and latency per tool, enabling cost-aware optimization decisions."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-per-tool-cost-attribution-tracking
tags: [cost-attribution, token-tracking, api-billing, tool-economics, spend-analysis, cost-optimization]
symptoms:
  - "Total monthly LLM spend is known but no breakdown by tool or conversation type"
  - "Cannot determine which tools to optimize to reduce costs by 20%"
  - "Token consumption per tool call is never measured — only total session tokens"
  - "External API costs (search, embedding, etc.) are not tracked alongside LLM costs"
  - "High-cost outlier conversations are invisible without per-conversation attribution"
---

## Why This Happens

Billing dashboards show aggregate spend. Without instrumenting each tool call with cost data — tokens consumed, API calls made, external service charges — there is no way to attribute spend to specific agent behaviors. A tool that calls an expensive external API on every turn, or a retrieval tool that embeds queries unnecessarily, remains invisible until the monthly bill arrives. Per-tool cost attribution requires capturing cost at the call site and aggregating by tool name, conversation, and time period.

## Solution 1: Cost Record

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class CostCategory(str, Enum):
    LLM_INPUT_TOKENS = "llm_input_tokens"
    LLM_OUTPUT_TOKENS = "llm_output_tokens"
    EMBEDDING_TOKENS = "embedding_tokens"
    EXTERNAL_API_CALL = "external_api_call"
    COMPUTE = "compute"
    STORAGE = "storage"


@dataclass
class ToolCostRecord:
    tool_name: str
    conversation_id: str
    category: CostCategory
    units: float               # tokens, api calls, etc.
    unit_cost_usd: float       # cost per unit
    total_cost_usd: float      # units * unit_cost_usd
    latency_ms: float
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)
    call_id: str = ""
```

## Solution 2: Cost Rate Card

```python
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class ToolCostRates:
    """Defines how to compute cost for a specific tool."""
    tool_name: str
    cost_per_call_usd: float = 0.0
    cost_per_input_token_usd: float = 0.0
    cost_per_output_token_usd: float = 0.0
    cost_per_kb_usd: float = 0.0
    external_service: str = ""


class CostRateCard:
    """
    Registry of per-tool cost rates.
    Covers LLM token costs and external API costs.
    """

    # Default rates (USD) — update to match current provider pricing
    ANTHROPIC_CLAUDE_SONNET = ToolCostRates(
        tool_name="_llm_claude_sonnet",
        cost_per_input_token_usd=3.0 / 1_000_000,
        cost_per_output_token_usd=15.0 / 1_000_000,
    )

    OPENAI_EMBEDDING_ADA = ToolCostRates(
        tool_name="_embedding_ada",
        cost_per_input_token_usd=0.1 / 1_000_000,
    )

    def __init__(self):
        self._rates: Dict[str, ToolCostRates] = {}

    def register(self, rates: ToolCostRates) -> None:
        self._rates[rates.tool_name] = rates

    def get(self, tool_name: str) -> Optional[ToolCostRates]:
        return self._rates.get(tool_name)

    def compute_cost(
        self,
        tool_name: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        calls: int = 1,
        kb: float = 0.0,
    ) -> float:
        rates = self._rates.get(tool_name)
        if rates is None:
            return 0.0
        cost = (
            rates.cost_per_call_usd * calls
            + rates.cost_per_input_token_usd * input_tokens
            + rates.cost_per_output_token_usd * output_tokens
            + rates.cost_per_kb_usd * kb
        )
        return round(cost, 8)
```

## Solution 3: Per-Tool Cost Tracker

```python
import time
from collections import defaultdict
from threading import Lock
from typing import Dict, List, Optional


class PerToolCostTracker:
    """
    Accumulates cost records per tool and per conversation.
    Supports windowed queries for billing period analysis.
    """

    def __init__(self, max_records: int = 500_000):
        self._records: List[ToolCostRecord] = []
        self._max = max_records
        self._lock = Lock()

    def record(self, cost_record: ToolCostRecord) -> None:
        with self._lock:
            self._records.append(cost_record)
            if len(self._records) > self._max:
                self._records.pop(0)

    def _recent(self, window_seconds: float) -> List[ToolCostRecord]:
        cutoff = time.time() - window_seconds
        with self._lock:
            return [r for r in self._records if r.timestamp >= cutoff]

    def cost_by_tool(self, window_seconds: float = 86400.0) -> Dict[str, float]:
        records = self._recent(window_seconds)
        result: dict = defaultdict(float)
        for r in records:
            result[r.tool_name] += r.total_cost_usd
        return dict(sorted(result.items(), key=lambda x: x[1], reverse=True))

    def cost_by_conversation(self, window_seconds: float = 86400.0) -> Dict[str, float]:
        records = self._recent(window_seconds)
        result: dict = defaultdict(float)
        for r in records:
            result[r.conversation_id] += r.total_cost_usd
        return dict(sorted(result.items(), key=lambda x: x[1], reverse=True))

    def top_conversations_by_cost(self, top_n: int = 10, window_seconds: float = 86400.0) -> list:
        by_conv = self.cost_by_conversation(window_seconds)
        return [
            {"conversation_id": cid, "total_cost_usd": round(cost, 6)}
            for cid, cost in list(by_conv.items())[:top_n]
        ]

    def summary(self, window_seconds: float = 86400.0) -> dict:
        records = self._recent(window_seconds)
        by_tool = self.cost_by_tool(window_seconds)
        total = sum(by_tool.values())
        return {
            "window_seconds": window_seconds,
            "total_cost_usd": round(total, 6),
            "record_count": len(records),
            "unique_tools": len(by_tool),
            "cost_by_tool": {k: round(v, 6) for k, v in by_tool.items()},
            "top_tool": max(by_tool, key=by_tool.get) if by_tool else None,
        }
```

## Solution 4: Cost-Attributed Tool Wrapper

```python
import time
import uuid
from typing import Any, Callable, Dict, Optional


class CostAttributedToolWrapper:
    """
    Wraps tool execution to record cost attribution after each call.
    Estimates token costs from response metadata when available.
    """

    def __init__(
        self,
        tracker: PerToolCostTracker,
        rate_card: CostRateCard,
    ):
        self._tracker = tracker
        self._rate_card = rate_card

    async def call(
        self,
        tool_name: str,
        args: Dict[str, Any],
        fn: Callable,
        conversation_id: str = "",
        input_tokens: int = 0,
        expected_output_tokens: int = 0,
    ) -> Any:
        start = time.time()
        result = await fn(**args)
        latency_ms = round((time.time() - start) * 1000, 2)

        # Extract token counts from result if tool returns them
        actual_input = input_tokens
        actual_output = expected_output_tokens
        if isinstance(result, dict):
            actual_input = result.get("input_tokens", input_tokens)
            actual_output = result.get("output_tokens", expected_output_tokens)

        cost = self._rate_card.compute_cost(
            tool_name=tool_name,
            input_tokens=actual_input,
            output_tokens=actual_output,
            calls=1,
        )

        category = (
            CostCategory.LLM_INPUT_TOKENS if actual_input > 0
            else CostCategory.EXTERNAL_API_CALL
        )

        self._tracker.record(ToolCostRecord(
            tool_name=tool_name,
            conversation_id=conversation_id,
            category=category,
            units=actual_input + actual_output,
            unit_cost_usd=cost / max(actual_input + actual_output, 1),
            total_cost_usd=cost,
            latency_ms=latency_ms,
            call_id=str(uuid.uuid4())[:16],
        ))

        return result
```

## Solution 5: Cost Budget Enforcer

```python
import time
from typing import Dict, Optional


class ToolCostBudgetEnforcer:
    """
    Enforces per-conversation and per-tool daily cost budgets.
    Raises CostBudgetExceededError when a budget is exceeded.
    """

    def __init__(
        self,
        tracker: PerToolCostTracker,
        per_conversation_limit_usd: float = 1.0,
        per_tool_daily_limit_usd: float = 50.0,
        window_seconds: float = 86400.0,
    ):
        self._tracker = tracker
        self._conv_limit = per_conversation_limit_usd
        self._tool_limit = per_tool_daily_limit_usd
        self._window = window_seconds

    def check_conversation(self, conversation_id: str) -> None:
        by_conv = self._tracker.cost_by_conversation(self._window)
        conv_cost = by_conv.get(conversation_id, 0.0)
        if conv_cost >= self._conv_limit:
            raise CostBudgetExceededError(
                f"conversation '{conversation_id}' cost ${conv_cost:.4f} exceeds limit ${self._conv_limit:.2f}"
            )

    def check_tool(self, tool_name: str) -> None:
        by_tool = self._tracker.cost_by_tool(self._window)
        tool_cost = by_tool.get(tool_name, 0.0)
        if tool_cost >= self._tool_limit:
            raise CostBudgetExceededError(
                f"tool '{tool_name}' daily cost ${tool_cost:.4f} exceeds limit ${self._tool_limit:.2f}"
            )

    def check_all(self, tool_name: str, conversation_id: str) -> None:
        self.check_conversation(conversation_id)
        self.check_tool(tool_name)


class CostBudgetExceededError(Exception):
    pass
```

## Solution 6: Cost Attribution Dashboard

```python
import time


class CostAttributionDashboard:
    """
    Combines per-tool costs, top conversations, budget status,
    and trend analysis into a single billing health view.
    """

    def __init__(
        self,
        tracker: PerToolCostTracker,
        rate_card: CostRateCard,
    ):
        self._tracker = tracker
        self._rate_card = rate_card

    def render(self) -> dict:
        summary_1h = self._tracker.summary(window_seconds=3600.0)
        summary_24h = self._tracker.summary(window_seconds=86400.0)
        top_convs = self._tracker.top_conversations_by_cost(top_n=5)

        projected_monthly = summary_24h["total_cost_usd"] * 30
        return {
            "generated_at": time.time(),
            "last_1h": summary_1h,
            "last_24h": summary_24h,
            "projected_monthly_usd": round(projected_monthly, 2),
            "top_conversations_24h": top_convs,
            "alert": projected_monthly > 1000,   # alert if >$1000/month projected
        }
```

## Comparison

| Approach | Per-Tool Cost | Per-Conversation Cost | Budget Enforcement | Rate Card | Dashboard |
|---|---|---|---|---|---|
| PerToolCostTracker | Yes | Yes | No | No | No |
| CostRateCard | No | No | No | Yes | No |
| CostAttributedToolWrapper | Via tracker | Via tracker | No | Via rate card | No |
| ToolCostBudgetEnforcer | Via tracker | Via tracker | Yes | No | No |
| CostAttributionDashboard | Via tracker | Via tracker | No | No | Yes |

**Best for production**: Update `CostRateCard` rates monthly as provider pricing changes — stale rates produce inaccurate projections. Set `per_conversation_limit_usd=1.0` as a safeguard against runaway agentic loops that make hundreds of tool calls — a single conversation exceeding $1 is almost always a bug rather than legitimate usage. Use `top_conversations_by_cost` to identify the most expensive sessions for manual review: they often reveal prompting inefficiencies (unnecessary retrieval, redundant embedding calls) that can be fixed to reduce costs by 20–40%. Track `projected_monthly_usd` in the dashboard as the primary cost health signal — it converts point-in-time spend rates into a number that finance teams understand.
