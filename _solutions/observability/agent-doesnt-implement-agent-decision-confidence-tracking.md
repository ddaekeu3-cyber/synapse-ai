---
title: "Agent Doesn't Implement Agent Decision Confidence Tracking"
description: "Agents that never surface how confident they are in a decision or tool selection make it impossible to distinguish reliable outputs from uncertain ones. Without confidence tracking, operators cannot set review thresholds, callers cannot gate downstream actions, and there is no signal for when the agent is operating outside its knowledge boundary. Implement decision confidence tracking that captures self-reported confidence, tracks calibration over time, and alerts when low-confidence decisions are acted upon."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-agent-decision-confidence-tracking
tags: [confidence-tracking, decision-quality, calibration, uncertainty, self-reporting, decision-review]
symptoms:
  - "No way to distinguish high-confidence from low-confidence agent decisions"
  - "Downstream systems act on uncertain agent outputs with no gating mechanism"
  - "Agent hallucinations are indistinguishable from grounded answers in logs"
  - "No calibration data to tune confidence thresholds for automated action"
  - "On-call engineers cannot tell whether a bad outcome was from low-confidence decision"
---

## Why This Happens

Most agents return a plain text answer or a tool call result with no confidence signal. Extracting confidence requires either prompting the model to self-report a score, parsing hedging language from the response, or using log-probabilities if the provider exposes them. Without a systematic approach, confidence remains implicit — embedded in the prose as "I think" or "possibly" — and is never surfaced as a structured field that downstream systems can act on. Tracking confidence over time reveals whether the agent is well-calibrated (high confidence correlates with correctness) and which decision types are consistently uncertain.

## Solution 1: Decision Confidence Record

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ConfidenceSource(str, Enum):
    SELF_REPORTED = "self_reported"    # model stated a confidence score
    HEDGING_PARSE = "hedging_parse"    # inferred from hedging language
    LOG_PROB = "log_prob"              # derived from token log-probabilities
    DEFAULT = "default"                # no signal; assumed mid-confidence


@dataclass
class DecisionConfidenceRecord:
    decision_id: str
    session_id: str
    decision_type: str              # e.g. "tool_selection", "answer", "routing"
    confidence_score: float         # 0.0 – 1.0
    confidence_source: ConfidenceSource
    decision_summary: str           # short description of what was decided
    outcome: Optional[str] = None   # "correct" | "incorrect" | "unknown"
    recorded_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)
```

## Solution 2: Hedging Language Confidence Parser

```python
import re
from typing import List, Tuple


HEDGING_PATTERNS: List[Tuple[re.Pattern, float]] = [
    (re.compile(r"\b(certain|definitely|absolutely|clearly|undoubtedly)\b", re.I), 0.95),
    (re.compile(r"\b(confident|sure|clearly|certainly)\b", re.I), 0.85),
    (re.compile(r"\b(likely|probably|generally|typically|usually)\b", re.I), 0.70),
    (re.compile(r"\b(may|might|could|perhaps|possibly|sometimes)\b", re.I), 0.50),
    (re.compile(r"\b(uncertain|unsure|unclear|not sure|not certain)\b", re.I), 0.30),
    (re.compile(r"\b(unlikely|doubt|questionable|speculative)\b", re.I), 0.20),
    (re.compile(r"\b(unknown|cannot determine|no information|no data)\b", re.I), 0.10),
]

EXPLICIT_SCORE_PATTERN = re.compile(
    r"confidence[:\s]+([0-9]{1,3})%|confidence[:\s]+0?\.[0-9]+", re.I
)


class HedgingLanguageConfidenceParser:
    """
    Parses a model response text to extract a confidence score.
    Prefers explicitly stated scores; falls back to hedging language heuristics.
    """

    def parse(self, text: str) -> Tuple[float, ConfidenceSource]:
        # Try explicit numeric confidence first
        match = EXPLICIT_SCORE_PATTERN.search(text)
        if match:
            raw = match.group(0)
            num_match = re.search(r"([0-9]{1,3}(?:\.[0-9]+)?)", raw)
            if num_match:
                val = float(num_match.group(1))
                score = val / 100.0 if val > 1.0 else val
                return min(max(score, 0.0), 1.0), ConfidenceSource.SELF_REPORTED

        # Scan for hedging patterns — take the average of all hits
        scores = []
        for pattern, score in HEDGING_PATTERNS:
            if pattern.search(text):
                scores.append(score)

        if scores:
            return round(sum(scores) / len(scores), 3), ConfidenceSource.HEDGING_PARSE

        return 0.65, ConfidenceSource.DEFAULT  # neutral default
```

## Solution 3: Decision Confidence Tracker

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Dict, List, Optional, Tuple


class DecisionConfidenceTracker:
    """
    Records decision confidence observations and computes
    running statistics per decision type and per session.
    """

    def __init__(self, max_records: int = 10000):
        self._max = max_records
        self._records: Deque[DecisionConfidenceRecord] = deque()
        self._lock = Lock()

    def record(self, rec: DecisionConfidenceRecord) -> None:
        with self._lock:
            self._records.append(rec)
            if len(self._records) > self._max:
                self._records.popleft()

    def recent(
        self,
        window_seconds: float = 3600.0,
        decision_type: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> List[DecisionConfidenceRecord]:
        cutoff = time.time() - window_seconds
        with self._lock:
            results = [
                r for r in self._records
                if r.recorded_at >= cutoff
                and (decision_type is None or r.decision_type == decision_type)
                and (session_id is None or r.session_id == session_id)
            ]
        return results

    def stats(self, window_seconds: float = 3600.0) -> dict:
        records = self.recent(window_seconds)
        if not records:
            return {"window_seconds": window_seconds, "decisions": 0}

        scores = [r.confidence_score for r in records]
        by_type: Dict[str, List[float]] = {}
        for r in records:
            by_type.setdefault(r.decision_type, []).append(r.confidence_score)

        return {
            "window_seconds": window_seconds,
            "decisions": len(records),
            "mean_confidence": round(sum(scores) / len(scores), 3),
            "low_confidence_count": sum(1 for s in scores if s < 0.5),
            "by_type": {
                dt: {
                    "count": len(vals),
                    "mean": round(sum(vals) / len(vals), 3),
                }
                for dt, vals in by_type.items()
            },
        }
```

## Solution 4: Confidence Calibration Evaluator

```python
import math
from typing import List, Optional


class ConfidenceCalibrationEvaluator:
    """
    Measures how well confidence scores predict correctness using
    Expected Calibration Error (ECE). Requires records with known outcomes.
    """

    def __init__(self, n_bins: int = 10):
        self._n_bins = n_bins

    def ece(self, records: List[DecisionConfidenceRecord]) -> Optional[float]:
        """
        Returns Expected Calibration Error in [0, 1]. Lower is better.
        Only uses records where outcome is known (correct/incorrect).
        """
        known = [
            r for r in records
            if r.outcome in ("correct", "incorrect")
        ]
        if len(known) < 10:
            return None  # not enough data

        bins = [[] for _ in range(self._n_bins)]
        for r in known:
            idx = min(int(r.confidence_score * self._n_bins), self._n_bins - 1)
            bins[idx].append(r)

        ece_sum = 0.0
        for b in bins:
            if not b:
                continue
            avg_conf = sum(r.confidence_score for r in b) / len(b)
            avg_acc = sum(1 for r in b if r.outcome == "correct") / len(b)
            ece_sum += (len(b) / len(known)) * abs(avg_conf - avg_acc)

        return round(ece_sum, 4)

    def calibration_summary(
        self, records: List[DecisionConfidenceRecord]
    ) -> dict:
        ece = self.ece(records)
        return {
            "ece": ece,
            "calibration_quality": (
                "good" if ece is not None and ece < 0.05 else
                "acceptable" if ece is not None and ece < 0.15 else
                "poor" if ece is not None else
                "insufficient_data"
            ),
            "records_with_outcomes": sum(
                1 for r in records if r.outcome in ("correct", "incorrect")
            ),
        }
```

## Solution 5: Low-Confidence Decision Gate

```python
from typing import Any, Callable, Optional


class LowConfidenceDecisionGate:
    """
    Intercepts decisions below a confidence threshold and routes them
    to a review handler instead of allowing automatic action.
    """

    def __init__(
        self,
        auto_act_threshold: float = 0.75,
        review_handler: Optional[Callable[[DecisionConfidenceRecord, Any], None]] = None,
    ):
        self._threshold = auto_act_threshold
        self._review_handler = review_handler
        self._gated_count = 0
        self._passed_count = 0

    def evaluate(
        self,
        record: DecisionConfidenceRecord,
        proposed_action: Any,
    ) -> dict:
        if record.confidence_score >= self._threshold:
            self._passed_count += 1
            return {
                "action": "proceed",
                "confidence": record.confidence_score,
                "decision_id": record.decision_id,
            }
        else:
            self._gated_count += 1
            if self._review_handler:
                self._review_handler(record, proposed_action)
            return {
                "action": "review_required",
                "confidence": record.confidence_score,
                "decision_id": record.decision_id,
                "reason": f"confidence {record.confidence_score:.2f} below threshold {self._threshold}",
            }

    def gate_stats(self) -> dict:
        total = self._passed_count + self._gated_count
        return {
            "total_evaluated": total,
            "passed": self._passed_count,
            "gated_for_review": self._gated_count,
            "gate_rate": round(self._gated_count / max(total, 1), 4),
        }
```

## Solution 6: Decision Confidence Dashboard

```python
import time
from typing import Optional


class DecisionConfidenceDashboard:
    """
    Combines confidence statistics, calibration evaluation,
    and gate metrics into a single operational report.
    """

    def __init__(
        self,
        tracker: DecisionConfidenceTracker,
        calibration: ConfidenceCalibrationEvaluator,
        gate: LowConfidenceDecisionGate,
    ):
        self._tracker = tracker
        self._calibration = calibration
        self._gate = gate

    def render(self, window_seconds: float = 3600.0) -> dict:
        records = self._tracker.recent(window_seconds)
        return {
            "generated_at": time.time(),
            "confidence_stats": self._tracker.stats(window_seconds),
            "calibration": self._calibration.calibration_summary(records),
            "gate": self._gate.gate_stats(),
        }
```

## Comparison

| Approach | Score Extraction | Hedging Parse | Calibration | Low-Conf Gating | Dashboard |
|---|---|---|---|---|---|
| HedgingLanguageConfidenceParser | Yes (explicit + hedging) | Yes | No | No | No |
| DecisionConfidenceTracker | No | No | No (stores only) | No | No |
| ConfidenceCalibrationEvaluator | No | No | Yes (ECE) | No | No |
| LowConfidenceDecisionGate | No | No | No | Yes (threshold) | No |
| DecisionConfidenceDashboard | No | No | Via evaluator | Via gate | Yes |

**Best for production**: Prompt the model to append `Confidence: X%` to every decision output so `HedgingLanguageConfidenceParser` gets an explicit score rather than a heuristic one. Set `auto_act_threshold=0.75` for automated downstream actions — anything below that threshold should require human review or a clarifying follow-up. Feed `outcome` labels back into `DecisionConfidenceRecord` from downstream verification (e.g., did the retrieved document actually answer the question?) so `ConfidenceCalibrationEvaluator.ece()` has data to work with. An ECE above 0.15 means confidence scores are systematically miscalibrated and should not be used for gating decisions.
