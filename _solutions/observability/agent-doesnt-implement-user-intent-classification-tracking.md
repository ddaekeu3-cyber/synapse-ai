---
title: "Agent Doesn't Implement User Intent Classification Tracking"
description: "Agents that do not classify and track user intent cannot answer fundamental product questions: what are users actually trying to do, which intents succeed most often, which intents the agent handles poorly, or how intent distribution shifts over time. Without intent tracking, product improvements are driven by intuition rather than data. Implement user intent classification that labels each session with its primary intent and tracks completion rates and latency by intent class."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-user-intent-classification-tracking
tags: [intent-classification, user-intent, session-analytics, task-success-rate, intent-distribution, product-analytics]
symptoms:
  - "No data on what users are actually trying to accomplish with the agent"
  - "Product roadmap is based on assumptions rather than observed intent distribution"
  - "Cannot measure task completion rate by intent — success metrics are undifferentiated"
  - "High-volume intents that the agent handles poorly are invisible in aggregate metrics"
  - "Intent distribution shifts after a product change go undetected for weeks"
---

## Why This Happens

Agent telemetry typically captures request-level metrics (latency, error rate, token count) without any semantic classification of what the user was trying to do. Two sessions with identical latency and success status may represent completely different intents — one asking a factual question, another requesting a multi-step workflow. Without intent labels, it is impossible to segment performance metrics by use case, identify which intents drive the most value, or detect when a new product feature shifts the intent distribution. Intent classification adds a lightweight label to each session based on the first user message and enables all downstream metrics to be segmented by intent.

## Solution 1: Intent Class

```python
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class IntentClass:
    intent_id: str
    name: str
    description: str
    keywords: List[str] = field(default_factory=list)
    patterns: List[str] = field(default_factory=list)   # regex patterns
    parent_intent: Optional[str] = None   # for hierarchical classification


DEFAULT_INTENT_CLASSES = [
    IntentClass(
        intent_id="factual_lookup",
        name="Factual Lookup",
        description="User asks for a specific fact or piece of information",
        keywords=["what is", "who is", "when did", "where is", "define", "tell me about"],
    ),
    IntentClass(
        intent_id="task_execution",
        name="Task Execution",
        description="User wants the agent to perform a multi-step action",
        keywords=["create", "send", "update", "delete", "run", "execute", "generate", "write"],
    ),
    IntentClass(
        intent_id="analysis_request",
        name="Analysis Request",
        description="User wants data analyzed or compared",
        keywords=["analyze", "compare", "evaluate", "summarize", "review", "assess"],
    ),
    IntentClass(
        intent_id="troubleshooting",
        name="Troubleshooting",
        description="User is trying to fix a problem or diagnose an issue",
        keywords=["error", "not working", "broken", "fix", "why is", "how to solve", "issue", "bug"],
    ),
    IntentClass(
        intent_id="recommendation",
        name="Recommendation Request",
        description="User wants a suggestion or recommendation",
        keywords=["recommend", "suggest", "best", "which should", "what should I", "advice"],
    ),
    IntentClass(
        intent_id="unknown",
        name="Unknown",
        description="Intent could not be classified",
        keywords=[],
    ),
]
```

## Solution 2: Keyword Intent Classifier

```python
import re
from typing import List, Optional


class KeywordIntentClassifier:
    """
    Classifies user intent based on keyword matching in the first user message.
    Returns the best-matching IntentClass and a confidence score.
    """

    def __init__(self, intent_classes: List[IntentClass] = None):
        self._classes = {ic.intent_id: ic for ic in (intent_classes or DEFAULT_INTENT_CLASSES)}
        self._unknown = self._classes.get("unknown") or IntentClass(
            intent_id="unknown", name="Unknown", description="Unclassified"
        )

    def classify(self, user_message: str) -> tuple[IntentClass, float]:
        text = user_message.lower().strip()
        scores: dict = {}

        for intent_id, ic in self._classes.items():
            if intent_id == "unknown":
                continue
            score = 0.0
            for keyword in ic.keywords:
                if keyword.lower() in text:
                    score += 1.0
            for pattern in ic.patterns:
                if re.search(pattern, text, re.IGNORECASE):
                    score += 1.5
            if score > 0:
                scores[intent_id] = score

        if not scores:
            return self._unknown, 0.0

        best_id = max(scores, key=lambda k: scores[k])
        best_score = scores[best_id]
        confidence = min(best_score / max(len(self._classes[best_id].keywords), 1), 1.0)
        return self._classes[best_id], round(confidence, 4)
```

## Solution 3: Intent Session Record

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class SessionOutcome(str, Enum):
    COMPLETED = "completed"
    ABANDONED = "abandoned"
    ERRORED = "errored"
    TIMED_OUT = "timed_out"
    IN_PROGRESS = "in_progress"


@dataclass
class IntentSessionRecord:
    session_id: str
    intent_id: str
    intent_name: str
    confidence: float
    first_message: str
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    outcome: SessionOutcome = SessionOutcome.IN_PROGRESS
    turn_count: int = 0
    tool_calls: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> Optional[float]:
        if self.ended_at is None:
            return None
        return round((self.ended_at - self.started_at) * 1000, 2)
```

## Solution 4: Intent Tracking Store

```python
import time
from collections import defaultdict
from threading import Lock
from typing import Dict, List, Optional


class IntentTrackingStore:
    """
    Accumulates intent session records and supports per-intent
    aggregation for success rate and latency analysis.
    """

    def __init__(self, max_records: int = 10000):
        self._max = max_records
        self._records: List[IntentSessionRecord] = []
        self._lock = Lock()

    def record(self, session: IntentSessionRecord) -> None:
        with self._lock:
            self._records.append(session)
            if len(self._records) > self._max:
                self._records.pop(0)

    def recent(self, window_seconds: float = 3600.0) -> List[IntentSessionRecord]:
        cutoff = time.time() - window_seconds
        with self._lock:
            return [r for r in self._records if r.started_at >= cutoff]

    def by_intent(self, window_seconds: float = 3600.0) -> Dict[str, dict]:
        records = self.recent(window_seconds)
        grouped: Dict[str, List[IntentSessionRecord]] = defaultdict(list)
        for r in records:
            grouped[r.intent_id].append(r)

        result = {}
        for intent_id, sessions in grouped.items():
            completed = [s for s in sessions if s.outcome == SessionOutcome.COMPLETED]
            durations = [s.duration_ms for s in sessions if s.duration_ms is not None]
            costs = [s.cost_usd for s in sessions]
            result[intent_id] = {
                "intent_name": sessions[0].intent_name,
                "session_count": len(sessions),
                "completion_rate": round(len(completed) / max(len(sessions), 1), 4),
                "avg_duration_ms": round(sum(durations) / max(len(durations), 1), 2) if durations else None,
                "avg_cost_usd": round(sum(costs) / max(len(costs), 1), 6),
                "avg_turns": round(sum(s.turn_count for s in sessions) / max(len(sessions), 1), 2),
            }
        return result
```

## Solution 5: Intent Distribution Monitor

```python
import time
from typing import Dict, List


class IntentDistributionMonitor:
    """
    Tracks how intent distribution shifts over time.
    Alerts when a previously rare intent spikes or a common
    intent drops, indicating a product or user behavior change.
    """

    def __init__(
        self,
        store: IntentTrackingStore,
        shift_threshold_pct: float = 10.0,
    ):
        self._store = store
        self._threshold = shift_threshold_pct / 100.0
        self._baseline: Optional[Dict[str, float]] = None
        self._baseline_captured_at: Optional[float] = None

    def capture_baseline(self, window_seconds: float = 86400.0) -> Dict[str, float]:
        by_intent = self._store.by_intent(window_seconds)
        total = sum(v["session_count"] for v in by_intent.values())
        distribution = {
            intent_id: round(v["session_count"] / max(total, 1), 4)
            for intent_id, v in by_intent.items()
        }
        self._baseline = distribution
        self._baseline_captured_at = time.time()
        return distribution

    def detect_shifts(self, window_seconds: float = 3600.0) -> dict:
        if self._baseline is None:
            return {"status": "no_baseline"}

        by_intent = self._store.by_intent(window_seconds)
        total = sum(v["session_count"] for v in by_intent.values())
        current = {
            intent_id: round(v["session_count"] / max(total, 1), 4)
            for intent_id, v in by_intent.items()
        }

        shifts = []
        all_intents = set(list(self._baseline.keys()) + list(current.keys()))
        for intent_id in all_intents:
            baseline_pct = self._baseline.get(intent_id, 0.0)
            current_pct = current.get(intent_id, 0.0)
            delta = current_pct - baseline_pct
            if abs(delta) >= self._threshold:
                shifts.append({
                    "intent_id": intent_id,
                    "baseline_pct": round(baseline_pct * 100, 1),
                    "current_pct": round(current_pct * 100, 1),
                    "delta_pct": round(delta * 100, 1),
                    "direction": "up" if delta > 0 else "down",
                })

        return {
            "status": "shift_detected" if shifts else "stable",
            "shifts": shifts,
            "baseline_age_hours": round(
                (time.time() - self._baseline_captured_at) / 3600, 1
            ) if self._baseline_captured_at else None,
        }
```

## Solution 6: Intent Classification Dashboard

```python
import time


class UserIntentClassificationDashboard:
    """
    Combines per-intent performance metrics, distribution monitoring,
    and top/bottom performing intents into a product analytics report.
    """

    def __init__(
        self,
        store: IntentTrackingStore,
        monitor: IntentDistributionMonitor,
    ):
        self._store = store
        self._monitor = monitor

    def render(self, window_seconds: float = 3600.0) -> dict:
        by_intent = self._store.by_intent(window_seconds)
        distribution_shifts = self._monitor.detect_shifts(window_seconds)

        sorted_by_volume = sorted(
            by_intent.items(), key=lambda x: x[1]["session_count"], reverse=True
        )
        sorted_by_completion = sorted(
            by_intent.items(), key=lambda x: x[1]["completion_rate"]
        )

        return {
            "generated_at": time.time(),
            "window_seconds": window_seconds,
            "by_intent": by_intent,
            "top_volume_intents": [k for k, _ in sorted_by_volume[:5]],
            "lowest_completion_intents": [k for k, _ in sorted_by_completion[:3]],
            "distribution_shifts": distribution_shifts,
        }
```

## Comparison

| Approach | Intent Classification | Session Tracking | Per-Intent Aggregation | Distribution Monitoring | Dashboard |
|---|---|---|---|---|---|
| KeywordIntentClassifier | Yes (keyword) | No | No | No | No |
| IntentTrackingStore | No | Yes | Yes | No | No |
| IntentDistributionMonitor | No | Via store | Via store | Yes | No |
| UserIntentClassificationDashboard | No | No | No | No | Yes |

**Best for production**: Run the classifier on the first user message only — subsequent turns in a session are about the same intent, so re-classifying each turn adds noise without improving accuracy. Use confidence scores to flag low-confidence classifications for manual review, which populates a training set for upgrading to an LLM-based or embedding-based classifier. Capture the intent baseline weekly and compare against the hourly distribution to detect shifts — daily comparisons have too much variance, monthly comparisons detect shifts too late. Focus product improvements on intents appearing in `lowest_completion_intents` with high session volume — these represent the highest-impact opportunities.
