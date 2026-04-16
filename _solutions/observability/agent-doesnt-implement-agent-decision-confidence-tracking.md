---
title: "Agent Doesn't Implement Agent Decision Confidence Tracking"
description: "Agents that make routing, tool-selection, and response-quality decisions without tracking their own confidence produce opaque outputs: every response looks equally authoritative whether the agent had high certainty or was essentially guessing. Implement confidence tracking that captures self-assessed confidence scores at key decision points, correlates confidence against outcome quality, and alerts when the agent is operating in a low-confidence regime that warrants human review."
date: 2026-04-16
difficulty: advanced
category: observability
slug: agent-doesnt-implement-agent-decision-confidence-tracking
tags: [decision-confidence, self-assessment, uncertainty-tracking, human-escalation, calibration, confidence-scoring]
symptoms:
  - "Agent returns confident-sounding responses for queries outside its knowledge domain"
  - "No signal for when to escalate to a human — every response has the same certainty"
  - "Cannot tell from logs whether the agent hesitated or was certain on a given decision"
  - "Calibration is unknown — high-confidence decisions fail at the same rate as low-confidence ones"
  - "Users have no visibility into response uncertainty and treat all outputs equally"
---

## Why This Happens

LLMs produce tokens with the same syntactic confidence regardless of their actual epistemic state. Without explicit self-assessment — prompting the model to estimate its own certainty and capturing that estimate — there is no signal distinguishing "I know this well" from "I'm extrapolating from weak signals." Confidence tracking adds a structured self-assessment step at key decision points, stores the scores alongside the decisions, and computes calibration metrics (does high-confidence correlate with correct outcomes?) to validate whether the agent's self-assessment is reliable.

## Solution 1: Decision Record

```python
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class DecisionType(str, Enum):
    TOOL_SELECTION = "tool_selection"     # which tool to invoke
    RESPONSE_GENERATION = "response_generation"  # final answer quality
    ROUTING = "routing"                   # which agent or path to take
    CLASSIFICATION = "classification"     # input category determination
    RETRIEVAL_RELEVANCE = "retrieval_relevance"  # RAG relevance scoring
    CUSTOM = "custom"


class ConfidenceBand(str, Enum):
    VERY_HIGH = "very_high"    # 0.85–1.0
    HIGH = "high"              # 0.70–0.85
    MEDIUM = "medium"          # 0.50–0.70
    LOW = "low"                # 0.30–0.50
    VERY_LOW = "very_low"      # 0.0–0.30

    @classmethod
    def from_score(cls, score: float) -> "ConfidenceBand":
        if score >= 0.85:
            return cls.VERY_HIGH
        if score >= 0.70:
            return cls.HIGH
        if score >= 0.50:
            return cls.MEDIUM
        if score >= 0.30:
            return cls.LOW
        return cls.VERY_LOW


@dataclass
class AgentDecisionRecord:
    decision_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    decision_type: DecisionType = DecisionType.CUSTOM
    description: str = ""
    confidence_score: float = 0.5      # 0.0–1.0 self-assessed
    confidence_band: ConfidenceBand = ConfidenceBand.MEDIUM
    session_id: str = ""
    agent_id: str = ""
    model: str = ""
    outcome_correct: Optional[bool] = None   # set after outcome is known
    outcome_quality: Optional[float] = None  # 0.0–1.0 quality score
    rationale: str = ""
    alternatives_considered: List[str] = field(default_factory=list)
    recorded_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.confidence_band = ConfidenceBand.from_score(self.confidence_score)
```

## Solution 2: Confidence Extractor

```python
import re
from typing import Optional, Tuple


class ConfidenceExtractor:
    """
    Extracts a numerical confidence score from LLM-generated text.
    Supports structured formats (JSON confidence field, bracketed scores)
    and natural language patterns ("I am quite confident", "I'm not sure").
    """

    EXPLICIT_PATTERNS = [
        re.compile(r'"confidence"\s*:\s*([0-9.]+)', re.I),
        re.compile(r'\[confidence:\s*([0-9.]+)\]', re.I),
        re.compile(r'confidence(?:\s+score)?[:\s]+([0-9.]+)', re.I),
    ]

    LANGUAGE_SIGNALS = [
        (re.compile(r'\b(?:certainly|definitely|clearly|absolutely|I am certain)\b', re.I), 0.90),
        (re.compile(r'\b(?:very confident|highly confident|strong(?:ly)? believe)\b', re.I), 0.85),
        (re.compile(r'\b(?:confident|I believe|I think|most likely)\b', re.I), 0.75),
        (re.compile(r'\b(?:probably|likely|generally|typically)\b', re.I), 0.65),
        (re.compile(r'\b(?:possibly|might|could be|not sure|uncertain)\b', re.I), 0.45),
        (re.compile(r'\b(?:unlikely|doubt|I don.t know|unclear|speculative)\b', re.I), 0.30),
        (re.compile(r'\b(?:no idea|cannot determine|insufficient|I have no)\b', re.I), 0.15),
    ]

    @classmethod
    def extract(cls, text: str) -> Tuple[Optional[float], str]:
        """Returns (confidence_score or None, extraction_method)."""
        # Try explicit numeric extraction first
        for pattern in cls.EXPLICIT_PATTERNS:
            match = pattern.search(text)
            if match:
                try:
                    score = float(match.group(1))
                    if 0.0 <= score <= 1.0:
                        return round(score, 4), "explicit_numeric"
                    if 1.0 < score <= 100.0:
                        return round(score / 100.0, 4), "explicit_pct"
                except ValueError:
                    pass

        # Fall back to language signal matching (take the highest-signal match)
        best_score = None
        for pattern, score in cls.LANGUAGE_SIGNALS:
            if pattern.search(text):
                if best_score is None or abs(score - 0.5) > abs(best_score - 0.5):
                    best_score = score

        if best_score is not None:
            return round(best_score, 4), "language_signal"

        return None, "not_found"
```

## Solution 3: Decision Confidence Store

```python
import threading
import time
from collections import defaultdict
from typing import Dict, List, Optional


class DecisionConfidenceStore:
    """
    Accumulates decision records indexed by session, decision type, and band.
    Provides sliced views for calibration analysis.
    """

    def __init__(self, max_records: int = 50_000):
        self._records: List[AgentDecisionRecord] = []
        self._max = max_records
        self._lock = threading.Lock()

    def record(self, decision: AgentDecisionRecord) -> None:
        with self._lock:
            if len(self._records) >= self._max:
                self._records.pop(0)
            self._records.append(decision)

    def update_outcome(self, decision_id: str, correct: bool, quality: Optional[float] = None) -> None:
        with self._lock:
            for r in reversed(self._records):
                if r.decision_id == decision_id:
                    r.outcome_correct = correct
                    if quality is not None:
                        r.outcome_quality = quality
                    break

    def by_band(self, band: ConfidenceBand) -> List[AgentDecisionRecord]:
        return [r for r in self._records if r.confidence_band == band]

    def by_type(self, decision_type: DecisionType) -> List[AgentDecisionRecord]:
        return [r for r in self._records if r.decision_type == decision_type]

    def recent(self, n: int = 100) -> List[AgentDecisionRecord]:
        return list(reversed(self._records[-n:]))

    def calibration_data(self) -> Dict[str, dict]:
        """For each band: accuracy rate among decisions with known outcomes."""
        results: Dict[str, dict] = {}
        for band in ConfidenceBand:
            decisions = self.by_band(band)
            with_outcome = [d for d in decisions if d.outcome_correct is not None]
            if not with_outcome:
                results[band] = {"count": 0, "accuracy": None}
                continue
            accuracy = sum(1 for d in with_outcome if d.outcome_correct) / len(with_outcome)
            results[band] = {
                "count": len(with_outcome),
                "accuracy": round(accuracy, 4),
                "avg_confidence": round(
                    sum(d.confidence_score for d in with_outcome) / len(with_outcome), 4
                ),
            }
        return results
```

## Solution 4: Calibration Analyzer

```python
from typing import Dict, List, Optional


class CalibrationAnalyzer:
    """
    Evaluates whether the agent's confidence scores are well-calibrated.
    A calibrated agent should have ~70% accuracy on decisions scored 0.70,
    ~90% accuracy on decisions scored 0.90, etc.
    Reports Expected Calibration Error (ECE) as a summary statistic.
    """

    def __init__(self, store: DecisionConfidenceStore):
        self._store = store

    def expected_calibration_error(self, n_bins: int = 10) -> Optional[float]:
        records = [r for r in self._store.recent(10_000) if r.outcome_correct is not None]
        if len(records) < 20:
            return None

        bin_size = 1.0 / n_bins
        ece = 0.0
        for i in range(n_bins):
            lo = i * bin_size
            hi = lo + bin_size
            bin_records = [r for r in records if lo <= r.confidence_score < hi]
            if not bin_records:
                continue
            avg_conf = sum(r.confidence_score for r in bin_records) / len(bin_records)
            avg_acc = sum(1 for r in bin_records if r.outcome_correct) / len(bin_records)
            ece += (len(bin_records) / len(records)) * abs(avg_conf - avg_acc)

        return round(ece, 4)

    def report(self) -> dict:
        calibration = self._store.calibration_data()
        ece = self.expected_calibration_error()
        alerts = []

        if ece is not None and ece > 0.15:
            alerts.append({
                "type": "poor_calibration",
                "ece": ece,
                "message": f"ECE={ece:.3f} exceeds 0.15 — agent confidence scores are poorly calibrated.",
            })

        # Check if VERY_HIGH confidence decisions have <80% accuracy
        vh = calibration.get(ConfidenceBand.VERY_HIGH, {})
        if vh.get("count", 0) > 20 and vh.get("accuracy", 1.0) < 0.80:
            alerts.append({
                "type": "overconfident",
                "accuracy": vh["accuracy"],
                "message": "Agent is overconfident: VERY_HIGH band has <80% accuracy.",
            })

        return {
            "calibration_by_band": calibration,
            "expected_calibration_error": ece,
            "alerts": alerts,
        }
```

## Solution 5: Low-Confidence Escalation Manager

```python
import time
from typing import Callable, List, Optional


class LowConfidenceEscalationManager:
    """
    Triggers escalation (human review, additional retrieval, clarification request)
    when decisions fall below confidence thresholds.
    Prevents alert storms with per-session cooldowns.
    """

    def __init__(
        self,
        escalation_threshold: float = 0.40,
        consecutive_low_to_escalate: int = 2,
        cooldown_seconds: float = 300.0,
    ):
        self._threshold = escalation_threshold
        self._consecutive = consecutive_low_to_escalate
        self._cooldown = cooldown_seconds
        self._session_state: dict = {}
        self._handlers: List[Callable[[dict], None]] = []

    def add_handler(self, fn: Callable[[dict], None]) -> None:
        self._handlers.append(fn)

    def evaluate(self, decision: AgentDecisionRecord) -> Optional[dict]:
        session_id = decision.session_id
        state = self._session_state.setdefault(session_id, {
            "consecutive_low": 0,
            "last_escalated": 0,
        })

        if decision.confidence_score < self._threshold:
            state["consecutive_low"] += 1
        else:
            state["consecutive_low"] = 0
            return None

        if state["consecutive_low"] < self._consecutive:
            return None

        now = time.time()
        if now - state["last_escalated"] < self._cooldown:
            return None

        state["last_escalated"] = now
        escalation = {
            "type": "low_confidence_escalation",
            "session_id": session_id,
            "consecutive_low_decisions": state["consecutive_low"],
            "latest_decision_id": decision.decision_id,
            "latest_confidence": decision.confidence_score,
            "decision_type": decision.decision_type,
            "recommendation": "Consider requesting clarification or routing to human review.",
        }
        for h in self._handlers:
            try:
                h(escalation)
            except Exception:
                pass
        return escalation
```

## Solution 6: Confidence Tracking Dashboard

```python
import time


class AgentDecisionConfidenceDashboard:
    """Combines store summary, calibration analysis, and escalation signals."""

    def __init__(
        self,
        store: DecisionConfidenceStore,
        analyzer: CalibrationAnalyzer,
        escalation_manager: LowConfidenceEscalationManager,
    ):
        self._store = store
        self._analyzer = analyzer
        self._escalation = escalation_manager

    def render(self) -> dict:
        recent = self._store.recent(200)
        calibration_report = self._analyzer.report()

        band_dist: dict = {}
        for r in recent:
            band_dist[r.confidence_band] = band_dist.get(r.confidence_band, 0) + 1

        low_conf_pct = (
            (band_dist.get(ConfidenceBand.LOW, 0) + band_dist.get(ConfidenceBand.VERY_LOW, 0))
            / max(len(recent), 1)
        )

        alerts = list(calibration_report.get("alerts", []))
        if low_conf_pct > 0.20 and len(recent) > 20:
            alerts.append({
                "type": "high_low_confidence_rate",
                "rate": round(low_conf_pct, 4),
                "message": f"{low_conf_pct:.1%} of recent decisions are low-confidence — review query distribution.",
            })

        return {
            "generated_at": time.time(),
            "recent_decisions": len(recent),
            "confidence_band_distribution": band_dist,
            "low_confidence_rate": round(low_conf_pct, 4),
            "calibration": calibration_report,
            "alerts": alerts,
            "healthy": len(alerts) == 0,
        }
```

## Comparison

| Approach | Score Extraction | Calibration | Escalation Trigger | Outcome Correlation | Dashboard |
|---|---|---|---|---|---|
| ConfidenceExtractor | Yes (numeric + language) | No | No | No | No |
| DecisionConfidenceStore | No | Partial (band accuracy) | No | Yes | No |
| CalibrationAnalyzer | No | Yes (ECE) | No | Via store | No |
| LowConfidenceEscalationManager | No | No | Yes (threshold + consecutive) | No | No |
| AgentDecisionConfidenceDashboard | No | No | No | No | Yes |

**Best for production**: Instrument confidence scores at every key decision point — tool selection and final response generation are the highest-value capture points. Prompt the LLM explicitly: "On a scale of 0 to 1, how confident are you in this response? Respond with a JSON object containing a 'confidence' key." Use `ConfidenceExtractor` to parse the score from the output. Track calibration using `CalibrationAnalyzer.expected_calibration_error()`: ECE below 0.10 indicates good calibration; above 0.15 means the confidence scores are not reliable for routing or escalation decisions and require recalibration via prompt tuning.
