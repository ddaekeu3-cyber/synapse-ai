---
title: "Agent Doesn't Implement User Feedback Correlation with Agent Decisions"
description: "Agents that collect thumbs-up/thumbs-down signals without linking them to the specific tool calls, retrieval results, and prompt decisions that produced the rated response cannot act on feedback: a thumbs-down could mean wrong retrieval, wrong tool selection, poor reasoning, or bad formatting. Implement feedback correlation that ties every user rating to the full decision trace of the response it rates, enabling root-cause attribution of quality failures."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-user-feedback-correlation-with-agent-decisions
tags: [user-feedback, feedback-correlation, decision-tracing, quality-attribution, rlhf-signal, response-rating]
symptoms:
  - "Thumbs-down signals collected but no way to determine which decision caused the bad response"
  - "Feedback data cannot be joined to traces because there is no shared correlation ID"
  - "Quality regressions invisible until users churn — no leading signal from feedback"
  - "Cannot distinguish retrieval failures from reasoning failures from formatting failures"
  - "Feedback volume by tool or prompt version never measured"
---

## Why This Happens

User feedback is collected at the response level, but quality problems originate at the decision level: which documents were retrieved, which tools were called, which prompt template was active, which model version was used. Without a correlation ID that links the feedback event to the response's full execution trace, the thumbs-down is unattributable — the engineering team knows quality is bad but cannot determine where. Correlation requires generating a stable response ID at generation time, embedding it in the feedback UI, and storing the decision trace keyed to that ID so feedback events can be joined after the fact.

## Solution 1: Decision Trace Record

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class DecisionType(str, Enum):
    RETRIEVAL = "retrieval"
    TOOL_CALL = "tool_call"
    PROMPT_TEMPLATE = "prompt_template"
    MODEL_SELECTION = "model_selection"
    ROUTING = "routing"
    RERANKING = "reranking"


@dataclass
class DecisionEvent:
    decision_type: DecisionType
    key: str                   # e.g. tool name, template id, model id
    value: Any                 # the specific choice made
    latency_ms: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ResponseDecisionTrace:
    response_id: str           # stable ID embedded in feedback UI
    session_id: str
    query: str
    model_id: str
    decisions: List[DecisionEvent] = field(default_factory=list)
    retrieved_doc_ids: List[str] = field(default_factory=list)
    tool_calls: List[str] = field(default_factory=list)
    prompt_template_id: str = ""
    total_latency_ms: float = 0.0
    token_count: int = 0
    created_at: float = field(default_factory=time.time)

    def add_decision(self, event: DecisionEvent) -> None:
        self.decisions.append(event)
        if event.decision_type == DecisionType.TOOL_CALL:
            self.tool_calls.append(event.key)
        elif event.decision_type == DecisionType.RETRIEVAL:
            if isinstance(event.value, list):
                self.retrieved_doc_ids.extend(event.value)
```

## Solution 2: Response Decision Trace Store

```python
import json
import time
from threading import Lock
from typing import Dict, List, Optional


class ResponseDecisionTraceStore:
    """
    In-memory store for response decision traces, keyed by response_id.
    Traces expire after TTL to bound memory usage.
    Replace with Redis or a time-series database for multi-instance deployments.
    """

    def __init__(self, ttl_seconds: float = 86400.0, max_entries: int = 50000):
        self._traces: Dict[str, ResponseDecisionTrace] = {}
        self._timestamps: Dict[str, float] = {}
        self._ttl = ttl_seconds
        self._max = max_entries
        self._lock = Lock()

    def store(self, trace: ResponseDecisionTrace) -> None:
        with self._lock:
            self._evict_expired()
            if len(self._traces) >= self._max:
                # Evict oldest
                oldest = min(self._timestamps, key=self._timestamps.get)
                del self._traces[oldest]
                del self._timestamps[oldest]
            self._traces[trace.response_id] = trace
            self._timestamps[trace.response_id] = time.time()

    def get(self, response_id: str) -> Optional[ResponseDecisionTrace]:
        with self._lock:
            trace = self._traces.get(response_id)
            if trace is None:
                return None
            age = time.time() - self._timestamps.get(response_id, 0)
            if age > self._ttl:
                del self._traces[response_id]
                del self._timestamps[response_id]
                return None
            return trace

    def _evict_expired(self) -> None:
        now = time.time()
        expired = [rid for rid, ts in self._timestamps.items() if now - ts > self._ttl]
        for rid in expired:
            self._traces.pop(rid, None)
            self._timestamps.pop(rid, None)

    def size(self) -> int:
        with self._lock:
            return len(self._traces)
```

## Solution 3: User Feedback Event

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class FeedbackSignal(str, Enum):
    POSITIVE = "positive"     # thumbs up
    NEGATIVE = "negative"     # thumbs down
    NEUTRAL = "neutral"       # explicit neutral / skipped
    CORRECTION = "correction" # user provided corrected text


@dataclass
class UserFeedbackEvent:
    feedback_id: str
    response_id: str           # joins to ResponseDecisionTrace
    session_id: str
    signal: FeedbackSignal
    user_id: str = ""
    free_text: Optional[str] = None
    corrected_response: Optional[str] = None
    feedback_latency_ms: float = 0.0   # ms after response delivery
    created_at: float = field(default_factory=time.time)

    def is_negative(self) -> bool:
        return self.signal in (FeedbackSignal.NEGATIVE, FeedbackSignal.CORRECTION)
```

## Solution 4: Feedback-Decision Correlator

```python
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


@dataclass
class CorrelatedFeedbackRecord:
    feedback: UserFeedbackEvent
    trace: Optional[ResponseDecisionTrace]
    correlation_found: bool

    def decision_summary(self) -> dict:
        if not self.trace:
            return {}
        return {
            "model_id": self.trace.model_id,
            "prompt_template_id": self.trace.prompt_template_id,
            "tool_calls": self.trace.tool_calls,
            "retrieved_doc_count": len(self.trace.retrieved_doc_ids),
            "total_latency_ms": self.trace.total_latency_ms,
            "decisions": [
                {
                    "type": d.decision_type.value,
                    "key": d.key,
                    "value": str(d.value)[:100],
                }
                for d in self.trace.decisions
            ],
        }


class FeedbackDecisionCorrelator:
    """
    Joins incoming feedback events with their stored decision traces.
    Records unmatched feedback (for trace retention tuning).
    """

    def __init__(self, trace_store: ResponseDecisionTraceStore):
        self._store = trace_store
        self._correlated: List[CorrelatedFeedbackRecord] = []
        self._unmatched_count = 0

    def correlate(self, feedback: UserFeedbackEvent) -> CorrelatedFeedbackRecord:
        trace = self._store.get(feedback.response_id)
        record = CorrelatedFeedbackRecord(
            feedback=feedback,
            trace=trace,
            correlation_found=trace is not None,
        )
        self._correlated.append(record)
        if not record.correlation_found:
            self._unmatched_count += 1
        return record

    def negative_records(
        self, window_seconds: float = 3600.0
    ) -> List[CorrelatedFeedbackRecord]:
        cutoff = time.time() - window_seconds
        return [
            r for r in self._correlated
            if r.feedback.created_at >= cutoff and r.feedback.is_negative()
        ]

    def stats(self) -> dict:
        total = len(self._correlated)
        return {
            "total_correlated": total,
            "unmatched": self._unmatched_count,
            "match_rate": round(
                (total - self._unmatched_count) / max(total, 1), 4
            ),
        }
```

## Solution 5: Decision Attribution Analyzer

```python
import time
from collections import defaultdict
from typing import Dict, List


class DecisionAttributionAnalyzer:
    """
    Aggregates correlated negative feedback records to surface which
    decisions (tool calls, retrieval docs, prompt templates, models)
    are most frequently associated with negative user signals.
    """

    def __init__(self, correlator: FeedbackDecisionCorrelator):
        self._correlator = correlator

    def attribute(self, window_seconds: float = 3600.0) -> dict:
        negatives = self._correlator.negative_records(window_seconds)
        if not negatives:
            return {"window_seconds": window_seconds, "negative_count": 0}

        tool_counts: Dict[str, int] = defaultdict(int)
        template_counts: Dict[str, int] = defaultdict(int)
        model_counts: Dict[str, int] = defaultdict(int)
        latency_buckets: Dict[str, int] = defaultdict(int)

        for record in negatives:
            if not record.trace:
                continue
            for tool in record.trace.tool_calls:
                tool_counts[tool] += 1
            tmpl = record.trace.prompt_template_id or "unknown"
            template_counts[tmpl] += 1
            model_counts[record.trace.model_id] += 1
            lat = record.trace.total_latency_ms
            bucket = "<500ms" if lat < 500 else "<2s" if lat < 2000 else ">=2s"
            latency_buckets[bucket] += 1

        n = len(negatives)
        return {
            "window_seconds": window_seconds,
            "negative_count": n,
            "by_tool": dict(sorted(tool_counts.items(), key=lambda x: -x[1])),
            "by_prompt_template": dict(sorted(template_counts.items(), key=lambda x: -x[1])),
            "by_model": dict(model_counts),
            "by_latency_bucket": dict(latency_buckets),
            "most_attributed_tool": max(tool_counts, key=tool_counts.get, default=None),
            "most_attributed_template": max(template_counts, key=template_counts.get, default=None),
        }
```

## Solution 6: Feedback Correlation Dashboard

```python
import time


class FeedbackCorrelationDashboard:
    """
    Combines correlation stats, attribution analysis, and recent negative
    feedback details into a single operational view for quality review.
    """

    def __init__(
        self,
        correlator: FeedbackDecisionCorrelator,
        analyzer: DecisionAttributionAnalyzer,
        trace_store: ResponseDecisionTraceStore,
    ):
        self._correlator = correlator
        self._analyzer = analyzer
        self._store = trace_store

    def render(self, window_seconds: float = 3600.0) -> dict:
        negatives = self._correlator.negative_records(window_seconds)
        recent_details = []
        for record in negatives[-10:]:  # last 10 negative feedback items
            detail = {
                "feedback_id": record.feedback.feedback_id,
                "response_id": record.feedback.response_id,
                "signal": record.feedback.signal.value,
                "free_text": record.feedback.free_text,
                "correlation_found": record.correlation_found,
            }
            if record.trace:
                detail["decisions"] = record.decision_summary()
            recent_details.append(detail)

        return {
            "generated_at": time.time(),
            "trace_store_size": self._store.size(),
            "correlation_stats": self._correlator.stats(),
            "attribution": self._analyzer.attribute(window_seconds),
            "recent_negative_feedback": recent_details,
        }
```

## Comparison

| Approach | Trace Storage | Feedback Join | Attribution | Negative Signal | Dashboard |
|---|---|---|---|---|---|
| ResponseDecisionTraceStore | Yes (TTL eviction) | No | No | No | No |
| FeedbackDecisionCorrelator | No | Yes (response_id) | No | Yes | No |
| DecisionAttributionAnalyzer | No | Via correlator | Yes (tool/template/model) | Via correlator | No |
| FeedbackCorrelationDashboard | No | No | No | No | Yes |

**Best for production**: Store `ResponseDecisionTrace` in Redis with TTL = 7 days — users often rate responses hours after receiving them, so a 24-hour TTL will miss many correlations. Embed `response_id` in the SSE stream's `DONE` chunk metadata so the UI receives it automatically and can attach it to the feedback widget without extra API calls. Use `DecisionAttributionAnalyzer.attribute()` in weekly quality reviews: a single prompt template or retrieval source consistently appearing at the top of the negative attribution list is a clear signal for targeted improvement. Monitor `match_rate` from `FeedbackDecisionCorrelator.stats()` — below 80% means traces are expiring before feedback arrives and TTL should be increased.
