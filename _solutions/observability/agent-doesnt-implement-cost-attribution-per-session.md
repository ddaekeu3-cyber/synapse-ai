---
title: "Agent Doesn't Implement Cost Attribution Per Session"
description: "Agents that track total token usage without attribution cannot answer basic cost questions: which user sessions are most expensive, which tools drive the most token consumption, or whether a new prompt template reduced costs. Implement per-session cost attribution that records token usage and estimated cost for every LLM call and tool result, aggregates by session and user, and surfaces cost anomalies and high-cost session patterns."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-cost-attribution-per-session
tags: [cost-attribution, token-tracking, session-cost, billing, llm-cost, usage-analytics]
symptoms:
  - "Total monthly LLM spend is known but cannot be broken down by user, feature, or session"
  - "No way to identify which sessions are outliers consuming 100x the average token budget"
  - "A new prompt change is deployed but its cost impact cannot be measured against baseline"
  - "Tool results injected into context are not counted toward session token cost"
  - "Cost data exists only in the billing dashboard — not correlated with agent session IDs"
---

## Why This Happens

LLM providers report token usage in API responses, but agent frameworks typically discard or aggregate this data without linking it to the session, user, or operation that generated it. Without a per-session accumulator that captures usage from every LLM call and context injection, cost data exists only as a provider-level aggregate. The fundamental problem is that cost attribution requires instrumentation at the call site — every LLM invocation must capture the `usage` field from the response and record it against the current session before returning.

## Solution 1: Token Usage Record

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class UsageSource(str, Enum):
    LLM_CALL = "llm_call"
    TOOL_RESULT_INJECTION = "tool_result_injection"
    SYSTEM_PROMPT = "system_prompt"
    RETRIEVAL_INJECTION = "retrieval_injection"
    HISTORY_INJECTION = "history_injection"


@dataclass
class TokenUsageRecord:
    session_id: str
    user_id: str
    source: UsageSource
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    operation_name: str = ""
    tool_name: Optional[str] = None
    recorded_at: float = field(default_factory=time.time)

    @property
    def estimated_cost_usd(self) -> float:
        return _estimate_cost(self.model, self.prompt_tokens, self.completion_tokens)


# Cost table (USD per 1M tokens) — update as pricing changes
_COST_TABLE = {
    "claude-opus-4-6":    {"prompt": 15.0,  "completion": 75.0},
    "claude-sonnet-4-6":  {"prompt": 3.0,   "completion": 15.0},
    "claude-haiku-4-5":   {"prompt": 0.8,   "completion": 4.0},
    "gpt-4o":             {"prompt": 5.0,   "completion": 15.0},
    "gpt-4o-mini":        {"prompt": 0.15,  "completion": 0.6},
}


def _estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    rates = _COST_TABLE.get(model, {"prompt": 5.0, "completion": 15.0})
    cost = (prompt_tokens * rates["prompt"] + completion_tokens * rates["completion"]) / 1_000_000
    return round(cost, 8)
```

## Solution 2: Session Cost Accumulator

```python
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Dict, List, Optional


@dataclass
class SessionCostSummary:
    session_id: str
    user_id: str
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int
    estimated_cost_usd: float
    llm_call_count: int
    started_at: float
    last_activity_at: float
    by_model: Dict[str, dict]
    by_source: Dict[str, int]
    by_operation: Dict[str, dict]


class SessionCostAccumulator:
    """
    Accumulates token usage records for a single session.
    Provides a live cost summary at any point during the session.
    """

    def __init__(self, session_id: str, user_id: str):
        self.session_id = session_id
        self.user_id = user_id
        self._records: List[TokenUsageRecord] = []
        self._lock = Lock()
        self._started_at = time.time()

    def record(self, usage: TokenUsageRecord) -> None:
        with self._lock:
            self._records.append(usage)

    def summary(self) -> SessionCostSummary:
        with self._lock:
            records = list(self._records)

        if not records:
            return SessionCostSummary(
                session_id=self.session_id,
                user_id=self.user_id,
                total_prompt_tokens=0,
                total_completion_tokens=0,
                total_tokens=0,
                estimated_cost_usd=0.0,
                llm_call_count=0,
                started_at=self._started_at,
                last_activity_at=self._started_at,
                by_model={},
                by_source={},
                by_operation={},
            )

        by_model: dict = {}
        by_source: dict = {}
        by_operation: dict = {}

        for r in records:
            # by model
            if r.model not in by_model:
                by_model[r.model] = {"prompt": 0, "completion": 0, "cost_usd": 0.0}
            by_model[r.model]["prompt"] += r.prompt_tokens
            by_model[r.model]["completion"] += r.completion_tokens
            by_model[r.model]["cost_usd"] += r.estimated_cost_usd

            # by source
            by_source[r.source.value] = by_source.get(r.source.value, 0) + r.total_tokens

            # by operation
            op = r.operation_name or "unknown"
            if op not in by_operation:
                by_operation[op] = {"tokens": 0, "cost_usd": 0.0, "calls": 0}
            by_operation[op]["tokens"] += r.total_tokens
            by_operation[op]["cost_usd"] += r.estimated_cost_usd
            by_operation[op]["calls"] += 1

        llm_calls = sum(1 for r in records if r.source == UsageSource.LLM_CALL)

        return SessionCostSummary(
            session_id=self.session_id,
            user_id=self.user_id,
            total_prompt_tokens=sum(r.prompt_tokens for r in records),
            total_completion_tokens=sum(r.completion_tokens for r in records),
            total_tokens=sum(r.total_tokens for r in records),
            estimated_cost_usd=round(sum(r.estimated_cost_usd for r in records), 6),
            llm_call_count=llm_calls,
            started_at=self._started_at,
            last_activity_at=max(r.recorded_at for r in records),
            by_model={m: {k: round(v, 6) if isinstance(v, float) else v for k, v in d.items()} for m, d in by_model.items()},
            by_source=by_source,
            by_operation=by_operation,
        )
```

## Solution 3: Global Cost Registry

```python
import time
from threading import Lock
from typing import Dict, List, Optional


class GlobalCostRegistry:
    """
    Manages cost accumulators for all active sessions.
    Evicts sessions that have been idle beyond the TTL.
    """

    def __init__(self, session_ttl_seconds: float = 3600.0, max_sessions: int = 10000):
        self._ttl = session_ttl_seconds
        self._max = max_sessions
        self._sessions: Dict[str, SessionCostAccumulator] = {}
        self._lock = Lock()

    def get_or_create(self, session_id: str, user_id: str = "") -> SessionCostAccumulator:
        with self._lock:
            if session_id not in self._sessions:
                self._evict()
                self._sessions[session_id] = SessionCostAccumulator(session_id, user_id)
            return self._sessions[session_id]

    def record(self, usage: TokenUsageRecord) -> None:
        acc = self.get_or_create(usage.session_id, usage.user_id)
        acc.record(usage)

    def get_summary(self, session_id: str) -> Optional[SessionCostSummary]:
        with self._lock:
            acc = self._sessions.get(session_id)
        return acc.summary() if acc else None

    def top_sessions_by_cost(self, limit: int = 10) -> List[SessionCostSummary]:
        with self._lock:
            accumulators = list(self._sessions.values())
        summaries = [a.summary() for a in accumulators]
        return sorted(summaries, key=lambda s: s.estimated_cost_usd, reverse=True)[:limit]

    def _evict(self) -> None:
        if len(self._sessions) < self._max:
            return
        cutoff = time.time() - self._ttl
        stale = [
            sid for sid, acc in self._sessions.items()
            if not acc._records or acc._records[-1].recorded_at < cutoff
        ]
        for sid in stale[:max(1, len(stale))]:
            del self._sessions[sid]
```

## Solution 4: Cost Anomaly Detector

```python
import time
from typing import List, Optional


class SessionCostAnomalyDetector:
    """
    Detects sessions whose cost significantly exceeds the rolling average.
    Useful for catching runaway loops or unusually expensive queries.
    """

    def __init__(
        self,
        registry: GlobalCostRegistry,
        z_threshold: float = 3.0,
    ):
        self._registry = registry
        self._threshold = z_threshold

    def detect(self, window_sessions: int = 100) -> List[dict]:
        summaries = self._registry.top_sessions_by_cost(window_sessions)
        if len(summaries) < 5:
            return []

        costs = [s.estimated_cost_usd for s in summaries]
        mean = sum(costs) / len(costs)
        std = (sum((c - mean) ** 2 for c in costs) / len(costs)) ** 0.5

        if std < 1e-9:
            return []

        anomalies = []
        for summary in summaries:
            z = (summary.estimated_cost_usd - mean) / std
            if z >= self._threshold:
                anomalies.append({
                    "session_id": summary.session_id,
                    "user_id": summary.user_id,
                    "cost_usd": summary.estimated_cost_usd,
                    "z_score": round(z, 2),
                    "llm_call_count": summary.llm_call_count,
                    "total_tokens": summary.total_tokens,
                })
        return anomalies
```

## Solution 5: Cost Attribution Report Generator

```python
import time
from typing import List, Optional


class CostAttributionReportGenerator:
    """
    Produces a structured cost attribution report across all sessions
    in a time window, broken down by user, model, and operation.
    """

    def __init__(self, registry: GlobalCostRegistry):
        self._registry = registry

    def generate(self, top_n: int = 50) -> dict:
        top_summaries = self._registry.top_sessions_by_cost(top_n)

        total_cost = sum(s.estimated_cost_usd for s in top_summaries)
        by_user: dict = {}
        by_model: dict = {}

        for s in top_summaries:
            uid = s.user_id or "anonymous"
            by_user[uid] = round(by_user.get(uid, 0.0) + s.estimated_cost_usd, 6)
            for model, data in s.by_model.items():
                if model not in by_model:
                    by_model[model] = {"cost_usd": 0.0, "tokens": 0}
                by_model[model]["cost_usd"] = round(
                    by_model[model]["cost_usd"] + data.get("cost_usd", 0.0), 6
                )
                by_model[model]["tokens"] += data.get("prompt", 0) + data.get("completion", 0)

        return {
            "generated_at": time.time(),
            "sessions_analyzed": len(top_summaries),
            "total_cost_usd": round(total_cost, 6),
            "top_sessions": [
                {
                    "session_id": s.session_id,
                    "user_id": s.user_id,
                    "cost_usd": s.estimated_cost_usd,
                    "tokens": s.total_tokens,
                    "llm_calls": s.llm_call_count,
                }
                for s in top_summaries[:10]
            ],
            "by_user": dict(sorted(by_user.items(), key=lambda kv: kv[1], reverse=True)[:10]),
            "by_model": by_model,
        }
```

## Solution 6: Cost Attribution Dashboard

```python
import time


class CostAttributionDashboard:
    """
    Combines live cost registry state, anomaly detection, and attribution
    reports into a single operational view.
    """

    def __init__(
        self,
        registry: GlobalCostRegistry,
        anomaly_detector: SessionCostAnomalyDetector,
        report_generator: CostAttributionReportGenerator,
    ):
        self._registry = registry
        self._anomaly = anomaly_detector
        self._report = report_generator

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "attribution_report": self._report.generate(top_n=50),
            "anomalous_sessions": self._anomaly.detect(window_sessions=200),
        }
```

## Comparison

| Approach | Per-Session Accumulation | Per-Model Breakdown | Cost Anomaly Detection | Top-Session Ranking | Attribution Report |
|---|---|---|---|---|---|
| TokenUsageRecord | No | Per-record | No | No | No |
| SessionCostAccumulator | Yes | Yes (by_model) | No | No | No |
| GlobalCostRegistry | Via accumulators | Via summaries | No | Yes | No |
| SessionCostAnomalyDetector | No | No | Yes (z-score) | No | No |
| CostAttributionReportGenerator | No | Yes (aggregate) | No | Yes (top N) | Yes |
| CostAttributionDashboard | No | No | Via detector | Via registry | Via generator |

**Best for production**: Record usage from the raw API response `usage` field immediately after each LLM call — do not reconstruct token counts from the prompt text, as the model's tokenizer may count differently than your estimate. Keep `_COST_TABLE` as a configuration file rather than hard-coded constants so it can be updated without a deploy when providers change pricing. Set an alert on `SessionCostAnomalyDetector.detect()`: a session with a z-score above 3 almost always indicates a loop, a very long document, or a user attempting to exhaust the system. Emit `session_id` and `estimated_cost_usd` as structured log fields on every LLM response — this provides a queryable cost trail in your log system independent of the in-process registry.
