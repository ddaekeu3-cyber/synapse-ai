---
title: "Agent Doesn't Implement Multi-Model Routing Decision Logging"
description: "Agents that route requests across multiple LLM models — sending simple queries to a cheap fast model and complex tasks to a capable expensive model — make routing decisions that are invisible in telemetry. Without routing decision logs, engineers cannot determine whether the routing logic is working correctly, measure cost savings from routing, detect routing errors, or audit which model produced a given output. Implement structured logging for every routing decision with its rationale, selected model, and outcome."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-multi-model-routing-decision-logging
tags: [model-routing, routing-decision, multi-model, llm-routing, cost-attribution, routing-audit]
symptoms:
  - "No record of which model was selected for a given request and why"
  - "Cannot determine what fraction of requests are routed to expensive models vs. cheap models"
  - "Routing errors (wrong model selected for task complexity) are invisible until user complains"
  - "Cost savings from routing logic cannot be quantified — no before/after model attribution"
  - "Model output quality issues cannot be traced back to routing decisions"
---

## Why This Happens

Model routing is typically implemented as a conditional branch: `if complexity_score > threshold: use model_a else use model_b`. The branch executes and its result is used, but the decision — the score, the threshold, the selected model, and why — is not recorded as a structured event. Without this log, the routing layer is a black box. Engineers cannot tune thresholds, detect when the complexity scorer misfires, or prove that routing is saving money. Routing decision logging captures the full decision context as a structured record so routing behavior is auditable, tunable, and observable.

## Solution 1: Routing Decision Record

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class RoutingOutcome(str, Enum):
    SUCCESS = "success"
    FALLBACK = "fallback"       # routed model failed; fell back to another
    ERROR = "error"             # routing itself failed
    OVERRIDE = "override"       # routing overridden by explicit caller instruction


@dataclass
class RoutingDecisionRecord:
    request_id: str
    session_id: str
    selected_model: str
    routing_rule: str              # name of the rule that selected this model
    complexity_score: Optional[float] = None
    task_type: Optional[str] = None
    candidate_models: List[str] = field(default_factory=list)
    decided_at: float = field(default_factory=time.time)
    outcome: RoutingOutcome = RoutingOutcome.SUCCESS
    fallback_model: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    latency_ms: Optional[float] = None
    cost_usd: Optional[float] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
```

## Solution 2: Complexity Scorer

```python
import re
from typing import Any, Dict


class RequestComplexityScorer:
    """
    Estimates request complexity as a 0.0–1.0 score based on
    message length, question depth, tool requirement signals,
    and domain keywords. Used by routing rules to select models.
    """

    COMPLEX_KEYWORDS = {
        "analyze", "synthesize", "compare", "evaluate", "critique",
        "explain why", "design", "architect", "reason", "infer",
        "multiple", "comprehensive", "detailed", "step by step",
    }
    SIMPLE_KEYWORDS = {
        "what is", "define", "list", "translate", "summarize briefly",
        "yes or no", "simple", "quick",
    }

    def score(self, messages: list, tools_available: int = 0) -> float:
        if not messages:
            return 0.5

        last_user = next(
            (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        text = last_user.lower()
        word_count = len(text.split())

        # Length signal
        length_score = min(word_count / 200.0, 1.0)

        # Keyword signals
        complex_hits = sum(1 for kw in self.COMPLEX_KEYWORDS if kw in text)
        simple_hits = sum(1 for kw in self.SIMPLE_KEYWORDS if kw in text)
        keyword_score = min((complex_hits - simple_hits * 0.5) / 3.0, 1.0)
        keyword_score = max(0.0, keyword_score)

        # Tool usage signal
        tool_score = min(tools_available / 10.0, 1.0) * 0.3

        # History depth signal
        turn_count = len([m for m in messages if m.get("role") == "user"])
        history_score = min(turn_count / 10.0, 1.0) * 0.2

        raw = (length_score * 0.3 + keyword_score * 0.4 + tool_score + history_score)
        return round(min(1.0, max(0.0, raw)), 4)
```

## Solution 3: Model Router

```python
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class RoutingRule:
    name: str
    model: str
    min_complexity: float = 0.0
    max_complexity: float = 1.0
    task_types: List[str] = None   # None = match all
    priority: int = 0              # higher = checked first


class ModelRouter:
    """
    Selects a model based on complexity score and task type.
    Returns a RoutingDecisionRecord with the full decision context.
    """

    def __init__(
        self,
        rules: List[RoutingRule],
        default_model: str,
        scorer: RequestComplexityScorer,
    ):
        self._rules = sorted(rules, key=lambda r: r.priority, reverse=True)
        self._default = default_model
        self._scorer = scorer

    def route(
        self,
        messages: list,
        session_id: str = "",
        task_type: Optional[str] = None,
        tools_available: int = 0,
        override_model: Optional[str] = None,
    ) -> RoutingDecisionRecord:
        request_id = str(uuid.uuid4())[:16]
        complexity = self._scorer.score(messages, tools_available)

        if override_model:
            return RoutingDecisionRecord(
                request_id=request_id,
                session_id=session_id,
                selected_model=override_model,
                routing_rule="override",
                complexity_score=complexity,
                task_type=task_type,
                candidate_models=[r.model for r in self._rules],
                outcome=RoutingOutcome.OVERRIDE,
            )

        for rule in self._rules:
            if not (rule.min_complexity <= complexity <= rule.max_complexity):
                continue
            if rule.task_types and task_type and task_type not in rule.task_types:
                continue
            return RoutingDecisionRecord(
                request_id=request_id,
                session_id=session_id,
                selected_model=rule.model,
                routing_rule=rule.name,
                complexity_score=complexity,
                task_type=task_type,
                candidate_models=[r.model for r in self._rules],
            )

        return RoutingDecisionRecord(
            request_id=request_id,
            session_id=session_id,
            selected_model=self._default,
            routing_rule="default_fallthrough",
            complexity_score=complexity,
            task_type=task_type,
            candidate_models=[r.model for r in self._rules],
        )
```

## Solution 4: Routing Decision Logger

```python
import json
import time
from typing import Callable, List, Optional


class RoutingDecisionLogger:
    """
    Records routing decisions and provides aggregate analysis.
    """

    def __init__(self, write_fn: Optional[Callable[[dict], None]] = None):
        self._write = write_fn or (lambda r: print(json.dumps(r)))
        self._records: List[RoutingDecisionRecord] = []
        self._max = 10000

    def log(self, record: RoutingDecisionRecord) -> None:
        self._records.append(record)
        if len(self._records) > self._max:
            self._records.pop(0)
        self._write({
            "event": "routing_decision",
            "request_id": record.request_id,
            "session_id": record.session_id,
            "selected_model": record.selected_model,
            "routing_rule": record.routing_rule,
            "complexity_score": record.complexity_score,
            "task_type": record.task_type,
            "outcome": record.outcome.value,
            "fallback_model": record.fallback_model,
            "cost_usd": record.cost_usd,
            "latency_ms": record.latency_ms,
            "decided_at": record.decided_at,
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r.decided_at >= cutoff]
        model_counts: dict = {}
        rule_counts: dict = {}
        total_cost = 0.0
        for r in recent:
            model_counts[r.selected_model] = model_counts.get(r.selected_model, 0) + 1
            rule_counts[r.routing_rule] = rule_counts.get(r.routing_rule, 0) + 1
            total_cost += r.cost_usd or 0.0
        return {
            "window_seconds": window_seconds,
            "total_decisions": len(recent),
            "by_model": model_counts,
            "by_rule": rule_counts,
            "fallbacks": sum(1 for r in recent if r.outcome == RoutingOutcome.FALLBACK),
            "overrides": sum(1 for r in recent if r.outcome == RoutingOutcome.OVERRIDE),
            "total_cost_usd": round(total_cost, 6),
        }
```

## Solution 5: Routing Accuracy Evaluator

```python
from typing import List


class RoutingAccuracyEvaluator:
    """
    Detects potential routing mismatches by comparing complexity scores
    to model selection — flags cases where a high-complexity request
    was routed to a low-capability model or vice versa.
    """

    def __init__(
        self,
        cheap_models: List[str],
        capable_models: List[str],
        complexity_threshold: float = 0.65,
    ):
        self._cheap = set(cheap_models)
        self._capable = set(capable_models)
        self._threshold = complexity_threshold

    def evaluate(self, records: List[RoutingDecisionRecord]) -> dict:
        overrouted = []   # complex request → cheap model
        underrouted = []  # simple request → expensive model

        for r in records:
            if r.complexity_score is None:
                continue
            if r.complexity_score >= self._threshold and r.selected_model in self._cheap:
                overrouted.append(r.request_id)
            elif r.complexity_score < self._threshold and r.selected_model in self._capable:
                underrouted.append(r.request_id)

        return {
            "evaluated": len(records),
            "overrouted_count": len(overrouted),
            "underrouted_count": len(underrouted),
            "overrouted_ids": overrouted[:10],
            "underrouted_ids": underrouted[:10],
            "mismatch_rate": round(
                (len(overrouted) + len(underrouted)) / max(len(records), 1), 4
            ),
        }
```

## Solution 6: Routing Dashboard

```python
import time


class MultiModelRoutingDashboard:
    """
    Combines logger summary, accuracy evaluation, and cost attribution
    into a single operational report for routing tuning.
    """

    def __init__(
        self,
        logger: RoutingDecisionLogger,
        evaluator: RoutingAccuracyEvaluator,
    ):
        self._logger = logger
        self._evaluator = evaluator

    def render(self, window_seconds: float = 3600.0) -> dict:
        summary = self._logger.summary(window_seconds)
        recent = [
            r for r in self._logger._records
            if r.decided_at >= time.time() - window_seconds
        ]
        accuracy = self._evaluator.evaluate(recent)
        return {
            "generated_at": time.time(),
            "routing_summary": summary,
            "accuracy": accuracy,
            "health": {
                "mismatch_rate": accuracy["mismatch_rate"],
                "fallback_rate": round(summary["fallbacks"] / max(summary["total_decisions"], 1), 4),
            },
        }
```

## Comparison

| Approach | Decision Recording | Complexity Scoring | Rule-Based Routing | Mismatch Detection | Cost Attribution |
|---|---|---|---|---|---|
| ModelRouter | Yes (record returned) | Via scorer | Yes | No | No |
| RequestComplexityScorer | No | Yes (0–1 score) | No | No | No |
| RoutingDecisionLogger | Yes (structured log) | No | No | No | Partial (sum) |
| RoutingAccuracyEvaluator | No | No | No | Yes | No |
| MultiModelRoutingDashboard | No | No | No | No | No |

**Best for production**: Log every routing decision with `complexity_score` and `routing_rule` — this is the primary dataset for threshold tuning. Set complexity thresholds based on observed score distributions from the first week of production traffic; do not guess thresholds from synthetic benchmarks. Monitor `mismatch_rate` via `RoutingAccuracyEvaluator`: above 0.10 means the scorer is unreliable for your traffic patterns and needs recalibration. Track `total_cost_usd` by model in `by_model` aggregates and compare to a baseline where all traffic uses the capable model — this quantifies the dollar value of routing.
